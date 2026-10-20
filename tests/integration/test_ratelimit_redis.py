"""The rate limiter against the Redis it actually runs on — 10 §6.4, S9.

**Marked ``security`` and not ``integration``, on purpose.** ``integration``
means "service + real PostgreSQL" (13 §2), and this file needs no database at
all: it is here rather than in ``tests/unit/`` because the thing under test is a
Redis-backed control, and ``LocMemCache`` cannot reach the branch that reads a
key's TTL. Adding a ``django_db`` marker to file it under ``integration`` would
be claiming a dependency it does not have and slowing the suite for nothing.
S9's obligation is a security one, and this is where it is discharged.

Counters are keyed on a per-test random identifier, so nothing here collides with
another run or another test — and the residue expires with its own window rather
than being left for whoever debugs the shared Redis next.
"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest
from django.conf import settings as django_settings
from django.core.cache import cache

from config.ratelimit import _key, consume, limit_for, reset

pytestmark = pytest.mark.security


class TestTheRealBackend:
    def test_django_redis_provides_the_ttl_the_limiter_reads(self) -> None:
        """The precondition ``_window_remaining`` depends on.

        The limiter reads ``cache.ttl`` when it exists and falls back to the full
        window when it does not — a graceful degradation, and therefore a silent
        one. This test makes it loud: if the cache backend is changed to one
        without ``ttl`` (locmem, a custom backend, a different Redis client), the
        ``Retry-After`` on every 429 quietly becomes an over-estimate, and this
        is the assertion that says so.
        """
        assert callable(getattr(cache, "ttl", None)), (
            "The configured cache backend has no `ttl`. config/ratelimit.py will "
            "fall back to reporting the whole window as the retry delay."
        )

    def test_the_limit_fires_at_the_configured_threshold(self) -> None:
        """S9, against the shipped table rather than a test-only value.

        ``pin_entry`` is the right scope for this: SEC-AUTH-002 says a 4-digit
        PIN's ~10⁴ space is protected "by rate limiting and lockout, not by hash
        strength alone", so the threshold this asserts is load-bearing rather
        than defensive.
        """
        who = f"pin-{uuid4().hex}"
        ceiling = limit_for("pin_entry").limit
        try:
            decisions = [consume("pin_entry", who) for _ in range(ceiling + 1)]

            assert [d.allowed for d in decisions[:-1]] == [True] * ceiling
            assert decisions[-1].allowed is False
            assert decisions[-1].remaining == 0
        finally:
            reset("pin_entry", who)

    def test_the_counter_survives_a_separate_consume_call(self) -> None:
        """The counter lives in Redis, not in this process.

        Asserted directly because the alternative — an in-memory counter — would
        pass every single-process test in this file while giving each gunicorn
        worker its own private, and therefore useless, allowance.
        """
        who = f"shared-{uuid4().hex}"
        try:
            for _ in range(limit_for("pin_entry").limit):
                consume("pin_entry", who)

            # A second call made after the first has finished must see the same
            # state; nothing is cached in a local variable between them.
            assert consume("pin_entry", who).allowed is False
        finally:
            reset("pin_entry", who)

    def test_retry_after_reflects_the_real_ttl(self) -> None:
        """``Retry-After`` is the time left, not the window's full length.

        Reaching this branch needs a backend with a TTL, which is the reason this
        file exists at all. The bound is one-sided: a client told to wait longer
        than necessary is a client that has been throttled by more than the
        policy says.
        """
        who = f"otp-{uuid4().hex}"
        window = limit_for("otp_issue").window_seconds
        try:
            for _ in range(limit_for("otp_issue").limit):
                assert consume("otp_issue", who).allowed is True

            refused = consume("otp_issue", who)

            assert refused.allowed is False
            assert 0 < refused.retry_after <= window
        finally:
            reset("otp_issue", who)

    def test_the_ttl_matches_the_configured_window(self) -> None:
        who = f"ttl-{uuid4().hex}"
        window = limit_for("otp_issue").window_seconds
        try:
            consume("otp_issue", who)

            ttl = cache.ttl(_key("otp_issue", who))

            # Redis rounds TTL to whole seconds, so a fresh key reads as the full
            # window and can read one second less if the clock ticked between the
            # write and the read.
            assert window - 1 <= ttl <= window
        finally:
            reset("otp_issue", who)

    def test_reset_removes_the_counter_from_redis(self) -> None:
        who = f"reset-{uuid4().hex}"
        for _ in range(limit_for("pin_entry").limit):
            consume("pin_entry", who)

        reset("pin_entry", who)

        # Checked before consuming again: a consume seeds the key, so asserting
        # absence after one would be asserting that the limiter does not work.
        # django-redis reports 0 for a key that does not exist.
        assert cache.ttl(_key("pin_entry", who)) == 0
        assert consume("pin_entry", who).allowed is True
        reset("pin_entry", who)

    @pytest.mark.slow
    def test_the_window_actually_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A one-second window, waited out for real.

        Marked ``slow`` because it sleeps. It is worth the second: an expiring
        window is the whole mechanism, and a limiter whose keys never expired
        would lock a member out permanently after their fifth wrong PIN — a
        security control that becomes a denial of service against the member it
        protects.
        """
        monkeypatch.setattr(
            django_settings,
            "RATE_LIMITS",
            {"probe": {"limit": 1, "window_seconds": 1}},
        )
        who = f"expiry-{uuid4().hex}"

        assert consume("probe", who).allowed is True
        assert consume("probe", who).allowed is False

        time.sleep(1.1)

        assert consume("probe", who).allowed is True
