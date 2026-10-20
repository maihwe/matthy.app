"""DRF throttles for the scopes in 10 §6.4.

The limiter itself is ``config.ratelimit``: one implementation, used by services
and HTTP alike, so a login limit and a read limit cannot be counted two
different ways.

Only the anonymous-read scope is wired here. The authenticated and
authentication-endpoint scopes are applied by the services that own them (a
login is keyed on IP+account, which is knowledge the login service has and a
view does not), and S4's auth API reuses this module for the same reason.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest
from rest_framework import exceptions as drf
from rest_framework.throttling import BaseThrottle

from config import ratelimit

__all__ = ["AnonymousReadThrottle", "client_ip"]


def client_ip(request: HttpRequest) -> str:
    """The caller's address as the server sees it.

    ``REMOTE_ADDR`` only. ``X-Forwarded-For`` is a header the client writes, and
    trusting it before a known proxy rewrites it lets a caller choose their own
    rate-limit bucket — which is the limit being turned off by whoever it is
    aimed at.
    """
    return str(request.META.get("REMOTE_ADDR") or "unknown")


class AnonymousReadThrottle(BaseThrottle):
    """Anti-scraping on unauthenticated reads — 10 §6.4 ``anonymous_read``.

    Authenticated callers are exempt: their reads are already attributable, and
    counting them here would put a shared limit in front of every member behind
    one NAT. The scope's own configuration fails *open* when Redis is down
    (P1-16): denying would turn a cache blip into a public outage, and the data
    is public either way.
    """

    scope = "anonymous_read"

    def allow_request(self, request: HttpRequest, view: Any) -> bool:
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            return True

        decision = ratelimit.consume(self.scope, client_ip(request))
        if decision.allowed:
            return True
        # DRF converts this to a 429; the project's exception handler adds the
        # envelope and the Retry-After header (04 §1.2).
        raise drf.Throttled(wait=decision.retry_after)
