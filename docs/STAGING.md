# Staging preparation

The backend staging image uses `docker/Dockerfile` and
`config.settings.staging`. Staging inherits production security settings.
Hosting, domain, external database/Redis services and real delivery providers
must be selected before a hosted walkthrough can start.

## Current state

- GitHub's `staging` environment is created; deployments are limited to protected
  branches (`main` and `develop`). No hosting resources or environment secrets
  were provisioned by this preparation.
- The Python base image is pinned to a registry-resolved digest. Production
  dependencies are generated with hashes in `requirements/lock/prod.txt`; the
  production input includes the base application dependencies. The generated lock
  has 55 pins and 950 release hashes, passes pip's hash-mode dry run, and its
  dependency audit found no known vulnerabilities.
- `.dockerignore` excludes local environment files, keys, virtual environments,
  Git metadata, caches and data artifacts from the image context.
- Static assets are collected during the build with `config.settings.build`,
  which does not load runtime credentials or require PostgreSQL/Redis. Collection
  failures fail the image build. Production/staging serves the assets through
  WhiteNoise; application source remains owned by root and the server runs as UID
  1000. The image explicitly makes the entrypoint executable.
- The server supports the platform's `PORT` variable. Its readiness probe uses an
  HTTP loopback URL, a concrete allowed host and the trusted HTTPS marker; it
  rejects redirects, dependency failures, liveness responses and invalid JSON.
  Gunicorn access logs omit query strings.
- Sixteen local readiness-probe regression cases passed. Build-only static
  collection copied 128 assets and post-processed 384 outputs. The workspace
  cannot access its Docker daemon; actual image validation is provided by
  [the Container workflow](../.github/workflows/container.yml).

The Container workflow builds the production image, generates ephemeral CI-only
keys, explicitly applies migrations against PostgreSQL 16, checks deployment
settings and image exclusions/permissions, starts Gunicorn, verifies readiness and
admin CSS, then stops Redis and confirms readiness fails while liveness succeeds.
Check its `Staging image` result on the relevant PR/commit before deploying; these
CI containers are not a persistent hosted staging environment.

## Inputs needed for hosting

1. Hosting provider/account or an existing server, and deployment access.
2. Staging hostname/domain with HTTPS and a reverse proxy/load balancer that
   overwrites client-supplied `X-Forwarded-Proto` before forwarding to Django.
   Public traffic should enter through that trusted TLS proxy.
3. PostgreSQL 16 and Redis endpoints accessible from the app, with credentials
   supplied through the hosting platform's secret store.
4. Container registry/deployment approach, a controlled migration/release step,
   and the previous release/database backup for rollback planning.
5. SMS/email provider accounts and sender configuration. The existing delivery
   interface still needs a real adapter and frontend email-link/invitation
   handling for the full authentication/group walkthrough.

The provider-independent image is ready to be tested before these choices are
made. No particular cloud account, domain or billing commitment is implied by the
configuration files.

## Configure the application

Use [.env.staging.example](../.env.staging.example) as the variable-name checklist;
its blank secret fields must be populated in the host's secret store. Do not
commit a filled file. Django refuses to boot without required application/database
settings, and token/MFA/delivery flows fail closed without their configuration.

Set these deliberately:

- `DJANGO_SETTINGS_MODULE=config.settings.staging` and `DJANGO_DEBUG=false`.
- `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` for the actual staging
  hostname. `HEALTHCHECK_HOST` must be a concrete allowed hostname; otherwise the
  image probe uses the first entry in `DJANGO_ALLOWED_HOSTS`.
- A separate staging `DJANGO_SECRET_KEY`, PostgreSQL credentials and `REDIS_URL`.
- Matching RSA PEM `JWT_SIGNING_KEY`/`JWT_VERIFYING_KEY` with at least 2048 bits,
  and a Fernet `MFA_ENCRYPTION_KEYS` keyring. JWT variables accept literal `\n`
  escapes; retain these keys across releases so credentials/tokens do not break
  merely because the image changes.
- Staging-specific JWT issuer/audience when separating it from another deployment.
- A configured `PHONE_VERIFICATION_BACKEND` with `check_available()` and
  `send(destination=..., code=..., purpose=...)`. The local capture backend refuses
  production/staging settings. Until a real adapter is supplied, registration/code
  delivery cannot complete the hosted walkthrough.
- `PORT` (default 8000), worker count/timeouts and the host's resource limits.

Client-IP/risk-signal handling is unchanged. Confirm the proxy topology before
changing trusted IP headers or claiming risk-based authentication is complete.

## Build and release

Build from the exact committed revision being released:

```sh
staging_revision="$(git rev-parse HEAD)"
docker build -f docker/Dockerfile -t "ihweza/app:${staging_revision}" .
```

The Dockerfile defaults to the hashed production lock. Development Compose
explicitly selects development dependencies for its separate local stack.
To regenerate the production lock with Python 3.12 and pip-tools:

```sh
pip-compile --generate-hashes --strip-extras --no-emit-index-url \
  --no-emit-trusted-host --output-file requirements/lock/prod.txt requirements/prod.txt
```

Use the provider's release job to run `python manage.py migrate --noinput` with
staging configuration **before** routing traffic to the new web process. The
entrypoint does not run migrations or collect static files at startup. Review
populated-table migration timing, backfill behavior and backup/restore plans first;
data backfill/seed reversals are no-ops rather than lossless rollback.

The default role is `web` (Gunicorn/ASGI). The same image supports `worker` and
`beat`; run at most one beat instance. Use `python manage.py check --deploy
--fail-level WARNING` for the configured release. Verify `/health/ready/` returns
200 through the real HTTPS staging hostname, `/static/admin/css/base.css` loads,
and `/admin/login/` renders before the account/group walkthrough.

Complete the acceptance cases in
[PROJECT_HANDOVER.md](PROJECT_HANDOVER.md#next-milestone-staging-and-a-foundation-walkthrough)
using synthetic accounts. Record the deployed URL, exact commit/image tag,
migration result, delivery-provider status and walkthrough outcomes in the
handover. No financial operations are part of this milestone.
