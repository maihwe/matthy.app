"""Settings shared by every environment.

Split per 02 §4: ``base`` holds what is true everywhere, and each environment
module imports ``*`` from it and states only its differences. An environment
module that restates a base value is a place the two can drift.

Nothing in this file reads a default for a secret. 10 §7 and 12 §4: a missing
required variable fails the boot loudly rather than defaulting to something
insecure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config.env import env, env_bool, env_int, env_list, require_env

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
SECRET_KEY = require_env("DJANGO_SECRET_KEY")
"""SEC-SEC-007: long, random, unique per environment. Its compromise is a
security incident — it forges sessions and signed tokens, so it is required
rather than defaulted, in every environment including tests."""

DEBUG = env_bool("DJANGO_DEBUG", default=False)
"""False unless explicitly turned on. 12 §4: DEBUG is False in staging and
production, asserted at boot; a debug page in production exposes settings,
source, and often credentials."""

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# ---------------------------------------------------------------------------
# Applications
#
# Project apps arrive with the slice that builds them (see PHASE_1_PLAN §3).
# Each slice adds its app here and ships its migration in the same commit, so
# an app is never installed without its schema.
#
# `django.contrib.postgres` is installed from the start because the schema
# depends on it: CITEXT-style case-insensitive uniqueness, trigram indexes for
# location search (03 §4.5, FR-LOC-003), and JSONB fields throughout.
# ---------------------------------------------------------------------------
DJANGO_APPS = [
    "config.admin.MFAAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "drf_spectacular",
]

LOCAL_APPS: list[str] = [
    # Slice S1. `accounts` must be the first project module: AUTH_USER_MODEL is
    # declared below and cannot be changed once `auth` and `admin` have been
    # migrated (PHASE_1_PLAN P1-5).
    "apps.accounts",
    # Slice S2. L0, and it depends on nothing: every module writes to it, so it
    # cannot import from any of them (PHASE_1_PLAN P1-5a).
    "apps.audit",
    # Slice S3. L0. `accounts.UserProfile.location_id` references it, and so
    # will `groups`; the module itself imports nothing from either.
    "apps.locations",
    "apps.groups",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # First after security, so that every log line written while handling the
    # request — including one written by a later middleware's failure — carries
    # the correlation ID (DEP-LOG-002).
    "config.middleware.CorrelationIdMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.accounts.middleware.BrowserMFAMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]
# SEC-XSS-001: autoescaping is never disabled. Any `|safe` is justified in a
# comment and reviewed.

# ---------------------------------------------------------------------------
# Database — NFR-DATA-001
#
# PostgreSQL, and the only source of truth for money (03 §1, D1). Redis holds
# caches and queues, never balances.
#
# ATOMIC_REQUESTS stays False deliberately. 02 §5.3 requires an explicit
# transaction boundary around each financial operation, and a blanket
# per-request transaction would hold locks across the request lifetime —
# including across any external call made during it, which §5.3 forbids.
# ---------------------------------------------------------------------------
DATABASES: dict[str, dict[str, Any]] = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": require_env("POSTGRES_DB"),
        "USER": require_env("POSTGRES_USER"),
        "PASSWORD": require_env("POSTGRES_PASSWORD"),
        "HOST": env("POSTGRES_HOST", default="localhost"),
        "PORT": env("POSTGRES_PORT", default="5432"),
        "CONN_MAX_AGE": env_int("POSTGRES_CONN_MAX_AGE", default=60),
        "CONN_HEALTH_CHECKS": True,
    }
}

# 03 §1 D6: internal keys are BIGINT; the externally visible identifier is a
# separate `public_id` UUID. This is the default for every model that does not
# state otherwise.
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Authentication
#
# AUTH_USER_MODEL is set by the accounts slice, which is the first project
# module built. It cannot be set later: Django cannot swap the user model once
# `auth` and `admin` migrations have been applied, so the custom user must
# exist before the first `migrate` on any database.
# ---------------------------------------------------------------------------

# Set in the same change that created apps.accounts, which is now. See
# PHASE_1_PLAN P1-5: this line and the module's first migration must land
# together, and neither may be applied to a database that has already run
# `migrate` with the default user model — `admin.LogEntry` fixes a foreign key
# to whatever user model existed at that moment.
#
# "accounts" is the explicit app label from AccountsConfig, not the derived
# "apps_accounts".
AUTH_USER_MODEL = "accounts.User"

# S4: PEM keys supplied by the environment. Health, migrations and public reads
# can boot without them; issuing access tokens fails closed until both are set.
# Literal \n escapes support single-line environment variables and Compose .env.
JWT_SIGNING_KEY = (env("JWT_SIGNING_KEY", default="") or "").replace("\\n", "\n")
JWT_VERIFYING_KEY = (env("JWT_VERIFYING_KEY", default="") or "").replace("\\n", "\n")
JWT_ISSUER = env("JWT_ISSUER", default="ihweza")
JWT_AUDIENCE = env("JWT_AUDIENCE", default="ihweza-api")
PHONE_VERIFICATION_BACKEND = env("PHONE_VERIFICATION_BACKEND", default="")
MFA_ENCRYPTION_KEYS = env("MFA_ENCRYPTION_KEYS", default="") or ""

# SEC-AUTH-001: Argon2id first. The ordering matters — Django picks the first
# hasher in the list, and keeps the rest for verifying older hashes.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

# SEC-AUTH-004: minimum length plus a breached-password check. The breached
# check runs against a local corpus so that no plaintext is sent to a
# third-party service; the validator lands with the accounts slice.
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
        # Django's default attribute list is ("username", "first_name",
        # "last_name", "email"), and three of those four name something this
        # model either does not have or does not use. There is no `first_name`
        # or `last_name`; `username` is nullable and is not an identity here
        # (`USERNAME_FIELD` is `phone`); and `email` is optional. So the default
        # list would leave the validator checking, for most members, nothing at
        # all. It is replaced with the attributes that actually identify a
        # member in this system. Recorded as P1-23.
        #
        # The phone entry does real work even though `NumericPasswordValidator`
        # already refuses an entirely numeric password: a *mixed* password that
        # merely contains the number — `Okeke2348031234567` — is not numeric and
        # would otherwise pass, while the number itself is public (it is how a
        # collector finds a member) and therefore the first thing to try.
        "OPTIONS": {"user_attributes": ("username", "email", "full_name", "phone")},
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    # SEC-AUTH-004's breached-password check, satisfied locally: Django's
    # validator compares against a list of ~20,000 common passwords shipped
    # inside the package. No plaintext leaves the process, which is the
    # requirement's explicit condition.
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# SEC-SES-004/005. Browser MFA enforces both deadlines against its backing row.
SESSION_COOKIE_NAME = "ihweza_admin_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = env_int("SESSION_IDLE_TIMEOUT_SECONDS", default=60 * 30)
SESSION_ABSOLUTE_TIMEOUT_SECONDS = env_int("SESSION_ABSOLUTE_TIMEOUT_SECONDS", default=60 * 60 * 12)
SESSION_SAVE_EVERY_REQUEST = True  # makes SESSION_COOKIE_AGE an idle timeout
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
"""SEC-SES-006: sessions are stored server-side; the cookie carries only the key."""

CSRF_COOKIE_HTTPONLY = False  # the admin's JS must read it to send X-CSRFToken
CSRF_COOKIE_SAMESITE = "Lax"

# ---------------------------------------------------------------------------
# Django REST Framework and the API conventions of 04 §1
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    # Mobile uses JWT; the web admin uses the session cookie (02 §5.7). Both
    # resolve to the same user and permission layer beneath, so authorisation
    # logic exists once.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.accounts.authentication.SessionJWTAuthentication",
        "apps.accounts.authentication.AccountSessionAuthentication",
    ],
    # SEC-AUTHZ-001: default-deny. A view with no declared permission class is
    # a test failure, not an open endpoint.
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "config.api.pagination.CursorPagination",
    "PAGE_SIZE": 50,
    "EXCEPTION_HANDLER": "config.api.exceptions.envelope_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    # A JSON number for money is rejected on input (04 §1.3).
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DATETIME_FORMAT": "iso-8601",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "IHWEZA API",
    "DESCRIPTION": (
        "Community contribution, savings, borrowing & disbursement platform.\n\n"
        "Money is a decimal string in the currency's major unit (04 §1.3). "
        "Identifiers are UUIDs, never internal integer keys (04 §1)."
    ),
    # The API version moves independently of the application version and
    # changes only on a breaking contract change (11 §7, 04 §1.1).
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1",
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {
        "GroupStatusEnum": "apps.groups.constants.GroupStatus.choices",
        "MemberStatusEnum": "apps.groups.constants.MemberStatus.choices",
    },
}

# ---------------------------------------------------------------------------
# Cache and queue — Redis (02 §2)
#
# Redis holds caches and queues. It never holds a balance (02 §5.6), and it
# never holds an idempotency key (02 §5.2) — a cache that evicts a key turns
# "safe to retry" into "charges twice".
# ---------------------------------------------------------------------------
REDIS_URL = require_env("REDIS_URL")

CACHES: dict[str, dict[str, Any]] = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": env("CACHE_KEY_PREFIX", default="ihweza"),
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}

# 10 §6.4's table, reproduced. The document calls these "suggested defaults";
# they are values in settings precisely so an incident can be answered by
# changing them, which is what §6.4 asks for. Read by config/ratelimit.py.
RATE_LIMITS: dict[str, dict[str, int | str]] = {
    # "10 / 15 min per IP+account". Both halves matter: per-account alone
    # misses spraying across many accounts, per-IP alone misses a distributed
    # attack on one. The two are combined into one key by the login service.
    "login": {"limit": 10, "window_seconds": 15 * 60},
    # The pair limit alone gives one IP a new allowance for every account.
    "login_ip": {"limit": 50, "window_seconds": 15 * 60},
    # Refresh is unauthenticated and performs a database lookup (C7).
    "refresh": {"limit": 120, "window_seconds": 60},
    "registration_ip": {"limit": 50, "window_seconds": 60 * 60},
    "phone_verification_ip": {"limit": 120, "window_seconds": 60},
    "email_verification_ip": {"limit": 50, "window_seconds": 60 * 60},
    "email_verification": {"limit": 3, "window_seconds": 60 * 60},
    "email_verification_destination": {"limit": 3, "window_seconds": 60 * 60},
    "email_verification_confirm_ip": {"limit": 120, "window_seconds": 60},
    "password_reset_ip": {"limit": 50, "window_seconds": 60 * 60},
    "password_reset_confirm_ip": {"limit": 120, "window_seconds": 60},
    "credential_change": {"limit": 5, "window_seconds": 15 * 60},
    "mfa_setup": {"limit": 5, "window_seconds": 15 * 60},
    "mfa_verify": {"limit": 10, "window_seconds": 15 * 60},
    "mfa_challenge_ip": {"limit": 120, "window_seconds": 60},
    # SEC-OTP-004 gives two windows for one action, so there are two scopes.
    # Neither subsumes the other: the daily cap bounds SMS spend, and the
    # 15-minute cap bounds how fast one destination can be hammered.
    "otp_issue": {"limit": 3, "window_seconds": 15 * 60},
    "otp_issue_daily": {"limit": 10, "window_seconds": 24 * 60 * 60},
    # "5 / 15 min per code" — keyed on the code record, not the destination.
    # SEC-OTP-003's 3-attempt cap on the row (max_attempts) is the tighter
    # control and fires first; this bounds a code that somehow survives longer.
    "otp_verify": {"limit": 5, "window_seconds": 15 * 60},
    "pin_entry": {"limit": 5, "window_seconds": 15 * 60},
    "password_reset": {"limit": 3, "window_seconds": 60 * 60},
    # Protects against a client in a retry loop, not against an attacker. A
    # Redis outage therefore allows these rather than stopping the money path.
    "financial_write": {"limit": 60, "window_seconds": 60, "on_unavailable": "allow"},
    "bulk_send": {"limit": 1, "window_seconds": 60 * 60},
    # Anti-scraping on unauthenticated reads. Allowing it during a Redis outage
    # is the lesser evil: denying turns a cache blip into a public outage, and
    # the data behind it is public either way.
    "anonymous_read": {"limit": 100, "window_seconds": 60, "on_unavailable": "allow"},
}
"""Every scope in 10 §6.4. The two `on_unavailable: allow` entries are named
exceptions to the fail-closed default; the reasoning is in P1-16."""

# SEC-LOCK-001's backoff, read by apps/accounts/services/lockout.py. In settings
# for the same reason the table above is: the threshold is a number an incident
# may need to change without a deploy.
#
# The escalation has no numbers in 10 — "a backoff period that escalates with
# repetition" is all it says — so the shape is a decision and is recorded as
# P1-20. The cap is not a detail: an uncapped doubling would let anyone who knows
# a member's phone number hold the account closed indefinitely, which converts a
# public identifier into a denial of service against a real person.
LOCKOUT: dict[str, int] = {
    # SEC-LOCK-001's "default 5".
    "threshold": 5,
    # The first lockout. Short on purpose: at this point the likeliest
    # explanation is a member who has forgotten which password they used.
    "base_seconds": 60,
    # And the ceiling, reached at the tenth consecutive failure. Fifteen minutes
    # of a member's attention is a real cost, and it is bounded.
    "max_seconds": 15 * 60,
}
"""Read as a whole and validated by `lockout._configured()`; a missing key raises
rather than defaulting, for the reason P1-15 gives — a lockout control that
silently becomes "unlimited" is worse than one that fails loudly."""

CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = env("CELERY_TIMEZONE", default="Africa/Lagos")
# A worker that dies mid-task must not silently drop it. Financial tasks are
# idempotent (02 §5.5), so redelivery is safe by design.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_TRACK_STARTED = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = env_int("CELERY_TASK_TIME_LIMIT_SECONDS", default=600)
CELERY_TASK_SOFT_TIME_LIMIT = env_int("CELERY_TASK_SOFT_TIME_LIMIT_SECONDS", default=540)
CELERY_BEAT_SCHEDULE: dict[str, dict[str, str]] = {}
"""Scheduled work arrives with the modules that need it — contribution
reminders in Phase 2, reconciliation sweeps in Phase 3 (02 §5.5)."""

# ---------------------------------------------------------------------------
# Internationalisation
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-ng"
TIME_ZONE = env("DJANGO_TIME_ZONE", default="Africa/Lagos")
USE_I18N = True
USE_TZ = True
"""D13-adjacent: timestamps are stored with an offset. The collector's morning
run in Lagos and a provider's webhook in UTC must not be compared naively."""

# ---------------------------------------------------------------------------
# Static and media
# ---------------------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
# SEC-DATA-003: object storage for documents is private by default with
# short-lived signed URLs. The backend is swapped in the deployment slice that
# introduces document upload; the local filesystem backend is development only.

# ---------------------------------------------------------------------------
# Logging — DEP-LOG-001/002/003
#
# Structured JSON, shipped off-host. Correlation IDs propagate from request
# through Celery task to completion, so one logical operation is traceable
# across processes.
# ---------------------------------------------------------------------------
LOG_LEVEL = env("DJANGO_LOG_LEVEL", default="INFO")

LOGGING: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "config.logging.JsonFormatter"},
    },
    "filters": {
        "correlation_id": {"()": "config.logging.CorrelationIdFilter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "filters": ["correlation_id"],
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}

# DEP-LOG-003: logs never contain passwords, PINs, tokens, full account
# numbers, or unredacted PII. `config.logging.REDACTED_KEYS` is applied by the
# formatter, and the security test suite asserts no credential reaches a log
# record (10 §11, S1).
