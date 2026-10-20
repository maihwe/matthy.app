"""Health endpoints — 12 §7.3.

    /health/live/   the process is running
    /health/ready/  the process can serve

12 §7.3: *"No health check that only asserts the process is running. A `web`
container that cannot reach PostgreSQL, or a `worker` whose broker connection
has silently died, is unhealthy and must report as such."*

The split is what makes that enforceable. Liveness answers "should this
container be restarted?" and readiness answers "should this container receive
traffic?". A container that can serve but not reach the database is **not**
dead — restarting it achieves nothing and hides the real fault — so it fails
readiness and is drained, not killed.

The deployment gates on ``/health/ready/`` before draining old instances
(12 §6).
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import connections
from django.db.utils import OperationalError
from django.http import HttpRequest, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

__all__ = ["liveness", "readiness"]

logger = logging.getLogger(__name__)


@never_cache
@require_GET
def liveness(request: HttpRequest) -> JsonResponse:
    """The process is up. Deliberately checks nothing else."""
    return JsonResponse({"status": "ok", "check": "live"})


def _check_database() -> tuple[bool, str]:
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except OperationalError as exc:
        logger.error("Readiness: database unreachable", exc_info=exc)
        return False, "database unreachable"
    return True, "ok"


def _check_cache() -> tuple[bool, str]:
    """Redis is the cache, the broker, and the rate-limit store (10 §6.4).

    A readiness check that skipped it would report healthy while every rate
    limit in the platform silently failed open — which is a security failure,
    not a performance one.
    """
    from django.core.cache import cache

    try:
        cache.set("health:ready", "1", timeout=5)
        if cache.get("health:ready") != "1":
            return False, "cache read-back mismatch"
    except Exception as exc:
        logger.error("Readiness: cache unreachable", exc_info=exc)
        return False, "cache unreachable"
    return True, "ok"


@never_cache
@require_GET
def readiness(request: HttpRequest) -> JsonResponse:
    """The process can serve: every dependency it needs is reachable."""
    checks = {
        "database": _check_database(),
        "cache": _check_cache(),
    }
    payload: dict[str, Any] = {
        "status": "ok" if all(result[0] for result in checks.values()) else "unavailable",
        "check": "ready",
        "dependencies": {name: result[1] for name, result in checks.items()},
    }
    healthy = payload["status"] == "ok"
    return JsonResponse(payload, status=200 if healthy else 503)
