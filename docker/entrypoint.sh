#!/usr/bin/env bash
#
# Container entrypoint. The application code is identical across all four
# process roles (12 §2); this script is the only thing that differs.
#
# ── What this deliberately does NOT do ──────────────────────────────────────
# It does not run migrations. 12 §5.1 is explicit: migrations run as "a
# discrete, reversible-where-possible step, never implicitly on application
# start", because two containers starting simultaneously must not race to
# migrate. Migrations are run by CI or by an operator, as their own step.
#
# It does not run `collectstatic` either. In production that is a build-time or
# release-step concern, and doing it here would mean four containers racing to
# write the same files.

set -euo pipefail

role="${1:-web}"

log() {
    printf '%s [entrypoint] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

# Fail fast on a missing setting rather than starting in a half-configured
# state. `config.env.require_env` raises the same way once Django loads; this
# check catches it a few seconds earlier and with a clearer message.
if [[ -z "${DJANGO_SECRET_KEY:-}" ]]; then
    log "FATAL: DJANGO_SECRET_KEY is not set. See .env.example."
    exit 1
fi

case "${role}" in
    web)
        log "starting web (ASGI) on :${PORT:-8000}"
        exec gunicorn config.asgi:application \
            --worker-class uvicorn.workers.UvicornWorker \
            --bind "0.0.0.0:${PORT:-8000}" \
            --workers "${GUNICORN_WORKERS:-4}" \
            --timeout "${GUNICORN_TIMEOUT:-60}" \
            --graceful-timeout 30 \
            --access-logfile - \
            --access-logformat '%(h)s %(m)s %(U)s %(s)s %(L)s' \
            --error-logfile -
        ;;
    worker)
        # 02 §5.5: financial tasks are idempotent, so --loglevel INFO is worth
        # the verbosity — a redelivered task should be visible in the log.
        log "starting celery worker"
        exec celery -A config worker \
            --loglevel="${CELERY_LOG_LEVEL:-INFO}" \
            --concurrency="${CELERY_CONCURRENCY:-4}"
        ;;
    beat)
        # 12 §2: beat must never run more than one instance. Two schedulers
        # means every scheduled task fires twice — duplicated reminders at
        # best, duplicated financial sweeps at worst. The deployment enforces
        # the singleton; this branch only refuses to be part of the mistake.
        log "starting celery beat (singleton — never scale this role)"
        exec celery -A config beat \
            --loglevel="${CELERY_LOG_LEVEL:-INFO}" \
            --schedule=/tmp/celerybeat-schedule
        ;;
    shell)
        exec python manage.py shell
        ;;
    *)
        log "running explicit release command"
        exec "$@"
        ;;
esac
