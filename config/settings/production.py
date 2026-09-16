"""Production settings.

12 §1: *"Staging is production-shaped."* This module is therefore the reference
for what production is, and every hardening setting lives here rather than in
a separate file that can drift from it.

Two things are enforced at import rather than documented:

* ``DEBUG`` must be False. A debug page in production exposes settings, source,
  and often credentials (12 §4).
* Every origin and host is stated explicitly. There is no wildcard, and no
  default that quietly works.
"""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured

from config.env import env_int, env_list
from config.settings.base import *  # noqa: F403
from config.settings.base import DEBUG as _BASE_DEBUG

DEBUG = False

if _BASE_DEBUG:  # pragma: no cover - a boot-time guard, not a branch tests reach
    raise ImproperlyConfigured(
        "DJANGO_DEBUG must be False in production. Refusing to boot with the debug page "
        "enabled: it exposes settings, source, and often credentials (12 §4)."
    )

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")
if not ALLOWED_HOSTS:  # pragma: no cover - boot-time guard
    raise ImproperlyConfigured(
        "DJANGO_ALLOWED_HOSTS must list the production hostnames. There is no default: "
        "an empty ALLOWED_HOSTS that silently permits localhost is a misconfiguration "
        "that reaches production as a Host-header attack surface."
    )

CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

# ---------------------------------------------------------------------------
# Transport security — SEC-DATA-001, SEC-HDR-001
# ---------------------------------------------------------------------------
SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = env_int("DJANGO_HSTS_SECONDS", default=60 * 60 * 24 * 365)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

SECURE_CONTENT_TYPE_NOSNIFF = True
"""SEC-XSS-003: a JSON body is never sniffed as HTML."""

SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"

# SEC-HDR-003 and SEC-DATA-001's TLS floor are terminated at the load balancer;
# they are verified against a live scan before go-live (12 §11), not asserted
# here, because this process never sees the TLS handshake.

# ---------------------------------------------------------------------------
# Logging — DEP-LOG-001
#
# Structured JSON, shipped off-host with an application user that cannot
# rewrite them (10 §8, SEC-AUD-004). The shipping handler is attached by the
# deployment environment; the console handler stays so that container logs
# survive a shipping outage.
# ---------------------------------------------------------------------------
LOGGING["root"]["level"] = env("DJANGO_LOG_LEVEL", default="INFO")  # noqa: F405
