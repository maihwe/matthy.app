"""Rate limiting, in Redis — 10 §6.4, 04 §1 (``429`` + ``Retry-After``).

10 §6.4 specifies the scopes, the windows and the fact that limits "are
configuration, not constants, so they can be tightened in response to an
incident without a deploy". This module is the mechanism; ``settings.RATE_LIMITS``
is the configuration.

**Fixed windows, deliberately.** A sliding window or a token bucket is smoother
and needs more Redis work per request; a fixed window is one ``INCR``. §6.4's
limits are expressed as "N per window" — 3 per 15 minutes, 100 per minute — and a
fixed window implements exactly that sentence. The cost is the classic boundary
artefact: 3 requests at 14:59 and 3 more at 15:00 are 6 in two minutes. For a
control whose job is to bound SMS spend and slow credential stuffing, that is an
acceptable burst, and it is named here rather than discovered later.

**Identifiers are hashed before they become keys.** A Redis key is not a private
place: it appears in ``MONITOR`` output, in slow-log entries, in ``keyspace``
metrics and in any dump taken from the instance. Writing
``rl:otp_issue:+2348031234567`` would put a member's phone number in all four
(SEC-PII-001). The digest is enough to count with, and is not personal data.

**A limiter that cannot answer denies.** If Redis is unreachable, ``consume``
returns a refusal rather than an allowance — see ``Limit.on_unavailable`` and
P1-16 in ``docs/PHASE_1_PLAN.md``. The trade is deliberate and has a real cost:
a Redis outage stops logins rather than merely making them unthrottled.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final, Literal

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured

__all__ = [
    "Decision",
    "Limit",
    "RateLimiterUnavailable",
    "composite",
    "consume",
    "limit_for",
    "reset",
]

Unavailable = Literal["deny", "allow"]

_KEY_PREFIX: Final = "rl"
"""Namespaced under the cache's own ``KEY_PREFIX``, so the counters are
identifiable in Redis and cannot collide with cached objects."""

_DIGEST_LENGTH: Final = 32
"""Truncated to 128 bits. Long enough that two identifiers do not collide
(a collision would merge two members' counters, which is a silent loosening of
the limit) and short enough not to bloat every key."""


@dataclass(frozen=True, slots=True)
class Limit:
    """One scope's ceiling, from ``settings.RATE_LIMITS``."""

    limit: int
    window_seconds: int
    on_unavailable: Unavailable = "deny"


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of consuming one unit.

    ``retry_after`` is what 04 §1 puts in the ``Retry-After`` header on a 429.
    It is the number of seconds left in the window, not the full window, so a
    client that obeys it is not made to wait longer than the limit requires.
    """

    scope: str
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


def _configured() -> dict[str, dict[str, object]]:
    configured = getattr(settings, "RATE_LIMITS", None)
    if not isinstance(configured, dict):
        raise ImproperlyConfigured(
            "settings.RATE_LIMITS is missing. 10 §6.4 requires the limits to be "
            "configuration rather than constants, so an absent mapping is an "
            "unlimited platform rather than a defaulted one."
        )
    return configured


def limit_for(scope: str) -> Limit:
    """Look up a scope's limit.

    Raises:
        ImproperlyConfigured: if the scope is not configured. A missing scope is
            a typo, and treating it as "unlimited" would silently remove a
            security control instead of failing a boot.
    """
    raw = _configured().get(scope)
    if raw is None:
        known = ", ".join(sorted(_configured())) or "(none)"
        raise ImproperlyConfigured(
            f"No rate limit configured for scope {scope!r}. Known scopes: {known}. "
            "Add it to settings.RATE_LIMITS rather than calling the limiter without one."
        )
    try:
        limit = Limit(
            limit=int(raw["limit"]),  # type: ignore[arg-type]
            window_seconds=int(raw["window_seconds"]),  # type: ignore[arg-type]
            on_unavailable=str(raw.get("on_unavailable", "deny")),  # type: ignore[arg-type]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ImproperlyConfigured(
            f"Rate limit for scope {scope!r} is malformed: {raw!r}. Each scope needs "
            "an integer 'limit' and an integer 'window_seconds'."
        ) from exc
    if limit.limit < 1 or limit.window_seconds < 1:
        raise ImproperlyConfigured(
            f"Rate limit for scope {scope!r} must have a positive limit and window, "
            f"got {limit.limit} per {limit.window_seconds}s. A limit of zero is not a "
            "stricter limit; it is an outage."
        )
    return limit


def _key(scope: str, identifier: str) -> str:
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]
    return f"{_KEY_PREFIX}:{scope}:{digest}"


def _window_remaining(key: str, window_seconds: int) -> int:
    """Seconds until the window resets.

    django-redis adds a ``ttl`` method to the cache object that the base cache
    API does not have, so it is read defensively. This value only affects a
    response header, and a header is not worth failing a login over — hence the
    fallback to the full window rather than an exception.
    """
    ttl = getattr(cache, "ttl", None)
    if ttl is None:
        return window_seconds
    try:
        remaining = int(ttl(key))
    except Exception:  # noqa: BLE001 - see docstring: cosmetic value, never fatal
        return window_seconds
    return remaining if remaining > 0 else window_seconds


def composite(*parts: str) -> str:
    """Join the parts of a scope's key into one identifier.

    Needed because 10 §6.4 keys login on "IP+account", and the two halves have
    to be combined in a way that cannot be forged into a match. ``+`` or ``:``
    would not do: with a plain concatenation the pairs ``("1.2.3.4", "5678")``
    and ``("1.2.3.45", "678")`` produce the same string, so a caller who can
    choose one half can collide their counter with another's — which is the
    limit being silently loosened, not a cosmetic bug.

    ``\\x1f`` (ASCII unit separator) is the separator because it is a control
    character that cannot appear in a normalised phone number, an IP address or
    a UUID. The parts are joined, then hashed like any other identifier.
    """
    return "\x1f".join(parts)


def consume(scope: str, identifier: str) -> Decision:
    """Record one attempt in ``scope`` for ``identifier`` and say whether it may proceed.

    ``identifier`` is whatever the scope is keyed on — a destination phone
    number, a user's public id, an IP address. It is hashed before use; see the
    module docstring.
    """
    limit = limit_for(scope)
    key = _key(scope, identifier)

    try:
        # `add` is SET NX with a TTL: it both seeds the counter and starts the
        # window, in one round trip, and only for the first caller.
        if cache.add(key, 1, limit.window_seconds):
            return Decision(
                scope=scope,
                allowed=True,
                limit=limit.limit,
                remaining=limit.limit - 1,
                retry_after=0,
            )
        try:
            count = int(cache.incr(key))
        except ValueError:
            # The key expired between `add` and `incr` — a legitimate race at a
            # window boundary, and the only case where `incr` raises. Starting a
            # fresh window is correct: the previous one had, in fact, ended.
            if cache.add(key, 1, limit.window_seconds):
                return Decision(
                    scope=scope,
                    allowed=True,
                    limit=limit.limit,
                    remaining=limit.limit - 1,
                    retry_after=0,
                )
            count = int(cache.incr(key))
    except Exception as exc:  # noqa: BLE001 - the whole point of on_unavailable
        if limit.on_unavailable == "allow":
            return Decision(
                scope=scope,
                allowed=True,
                limit=limit.limit,
                remaining=limit.limit,
                retry_after=0,
            )
        raise RateLimiterUnavailable(scope) from exc

    if count > limit.limit:
        return Decision(
            scope=scope,
            allowed=False,
            limit=limit.limit,
            remaining=0,
            retry_after=_window_remaining(key, limit.window_seconds),
        )

    return Decision(
        scope=scope,
        allowed=True,
        limit=limit.limit,
        remaining=limit.limit - count,
        retry_after=0,
    )


def reset(scope: str, identifier: str) -> None:
    """Clear a counter.

    Used when the thing being counted has succeeded — a correct password, a
    verified OTP — because §6.4's limits are per window and a member who gets
    their password right should not stay throttled by their earlier mistakes.
    A failure to clear is logged by the cache backend and is not fatal: the
    counter expires on its own.
    """
    cache.delete(_key(scope, identifier))


class RateLimiterUnavailable(RuntimeError):
    """Redis could not answer, and the scope is configured to deny.

    A distinct exception rather than a returned refusal, because the two mean
    different things to an operator: a refusal is a client that hit its limit,
    and this is the platform's own control being down. 12's alerting treats the
    second as a P1 — the same way it treats a backup that has not run.
    """

    def __init__(self, scope: str) -> None:
        super().__init__(
            f"Rate limiter is unavailable for scope {scope!r} and the scope denies "
            "when it cannot answer (Limit.on_unavailable='deny'). Redis is the "
            "dependency; see PHASE_1_PLAN P1-16 for why this is a denial."
        )
        self.scope = scope
