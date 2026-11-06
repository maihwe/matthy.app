"""Rate limiter logic — 10 §6.4, and 10 §11 test obligation S9.

Marked ``unit``: these run against Django's in-process ``LocMemCache``, so they
need neither PostgreSQL nor Redis. The autouse fixture below replaces ``CACHES``
for the whole module precisely so that a test here cannot reach the real Redis
that ``config.settings.test`` configures — a unit test that quietly depends on a
running service is the failure mode ``unit`` exists to prevent.

The Redis-backed path is exercised for real in
``tests/integration/test_ratelimit_redis.py``; the split is deliberate, because
``LocMemCache`` has no ``ttl`` method and so cannot reach the branch that reads
one.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured

from config import ratelimit
from config.ratelimit import (
    Limit,
    RateLimiterUnavailable,
    _key,
    composite,
    consume,
    limit_for,
    reset,
)

pytestmark = pytest.mark.unit

LOCMEM = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "ratelimit-unit",
    }
}


@pytest.fixture(autouse=True)
def _locmem_cache(settings):  # type: ignore[no-untyped-def]
    """Isolate every test in this module from the real cache.

    Autouse rather than opt-in, because the test that forgets it is the one that
    writes counters into the shared Redis and fails the next run instead of this
    one.

    This is the one fixture in the suite that must keep pytest-django's
    ``settings`` fixture, and the ``type: ignore`` it costs. Everywhere else
    ``monkeypatch.setattr(django_settings, ...)`` is used instead, because it is
    annotated and needs no ignore. It cannot be used *here*:
    ``monkeypatch`` sets the attribute on the wrapped settings object, which
    misses Django's own ``setting_changed`` receiver, and that receiver is what
    rebuilds ``django.core.cache.caches``. Assigning ``CACHES`` through
    ``settings`` fires it, so the swap to LocMem actually takes effect;
    assigning it through ``monkeypatch`` would leave this module pointed at the
    real Redis while reading as though it were isolated — which is precisely the
    failure the autouse fixture exists to prevent, arriving by the fix rather
    than the omission.
    """
    settings.CACHES = LOCMEM


def _limited(limit: int, window: int = 900, **extra: Any) -> dict[str, dict[str, Any]]:
    """A one-scope RATE_LIMITS mapping, for tests that need their own ceiling."""
    return {"probe": {"limit": limit, "window_seconds": window, **extra}}


class TestConfiguration:
    """A limit that is absent or misspelled must fail loudly, not default open."""

    def test_a_scope_that_is_not_configured_raises(self) -> None:
        with pytest.raises(ImproperlyConfigured) as refused:
            limit_for("no_such_scope")

        # The message has to say what *is* configured, or the person reading the
        # traceback cannot tell a typo from a setting that never loaded.
        assert "no_such_scope" in str(refused.value)
        assert "login" in str(refused.value)

    def test_the_missing_setting_names_10_6_4(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(django_settings, "RATE_LIMITS")

        with pytest.raises(ImproperlyConfigured) as refused:
            limit_for("login")

        assert "10 §6.4" in str(refused.value)

    @pytest.mark.parametrize(
        "malformed",
        [
            {"limit": 3},  # no window at all
            {"window_seconds": 60},  # no limit at all
            {"limit": "three", "window_seconds": 60},  # not a number
            {"limit": 3, "window_seconds": "an hour"},  # not a number
            {"limit": None, "window_seconds": 60},  # not a number
        ],
    )
    def test_a_malformed_scope_raises(
        self, monkeypatch: pytest.MonkeyPatch, malformed: dict[str, Any]
    ) -> None:
        monkeypatch.setattr(django_settings, "RATE_LIMITS", {"probe": malformed})

        with pytest.raises(ImproperlyConfigured) as refused:
            limit_for("probe")

        assert "malformed" in str(refused.value)

    @pytest.mark.parametrize(
        ("limit", "window"),
        [(0, 900), (-1, 900), (3, 0)],
    )
    def test_a_non_positive_ceiling_raises(
        self, monkeypatch: pytest.MonkeyPatch, limit: int, window: int
    ) -> None:
        """A limit of zero is refused rather than honoured.

        It reads like "nothing may pass", which is a plausible thing to write
        during an incident and is really an outage: every request in the scope
        fails, including the ones the control exists to keep working.
        """
        monkeypatch.setattr(django_settings, "RATE_LIMITS", _limited(limit, window))

        with pytest.raises(ImproperlyConfigured) as refused:
            limit_for("probe")

        assert "positive" in str(refused.value)

    def test_the_shipped_table_is_the_one_10_6_4_specifies(self) -> None:
        """The configured ceilings, against the document.

        This is the assertion that makes the table a decision rather than a
        default. A change to any of these numbers is a change to a security
        control: it belongs in 10 §6.4 first, and in this list second.

        Two scopes are not table rows: ``otp_issue_daily`` comes from
        SEC-OTP-004's "3 per 15 minutes, 10 per day", which the §6.4 row states
        as one of its two windows, and ``otp_verify`` is keyed per code because
        that is what the row says.
        """
        assert django_settings.RATE_LIMITS == {
            "login": {"limit": 10, "window_seconds": 15 * 60},
            "login_ip": {"limit": 50, "window_seconds": 15 * 60},
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
            "otp_issue": {"limit": 3, "window_seconds": 15 * 60},
            "otp_issue_daily": {"limit": 10, "window_seconds": 24 * 60 * 60},
            "otp_verify": {"limit": 5, "window_seconds": 15 * 60},
            "pin_entry": {"limit": 5, "window_seconds": 15 * 60},
            "password_reset": {"limit": 3, "window_seconds": 60 * 60},
            "financial_write": {"limit": 60, "window_seconds": 60, "on_unavailable": "allow"},
            "bulk_send": {"limit": 1, "window_seconds": 60 * 60},
            "anonymous_read": {"limit": 100, "window_seconds": 60, "on_unavailable": "allow"},
        }

    def test_only_the_two_named_scopes_fail_open(self) -> None:
        """Fail-closed is the default; the exceptions are named, and only two.

        Written as an assertion over the whole table rather than as two checks on
        the two scopes, so that a third scope added with ``on_unavailable:
        allow`` trips this test and has to be justified — the default is the
        interesting property, not the exceptions.
        """
        failing_open = {
            scope
            for scope in django_settings.RATE_LIMITS
            if limit_for(scope).on_unavailable == "allow"
        }

        assert failing_open == {"financial_write", "anonymous_read"}


class TestKeying:
    """Keys are derived, never literal. A Redis key is not a private place."""

    def test_the_identifier_does_not_survive_into_the_key(self) -> None:
        phone = "+2348031234567"

        key = _key("otp_issue", phone)

        assert phone not in key
        # Neither the digits alone, in case a future change normalises first and
        # hashes after: the property is that no form of the identifier is in it.
        assert "2348031234567" not in key
        assert key.startswith("rl:otp_issue:")

    def test_the_scope_is_in_the_key_and_a_different_one_does_not_collide(self) -> None:
        assert _key("login", "x") != _key("pin_entry", "x")

    def test_the_same_identifier_is_stable_and_a_different_one_is_not(self) -> None:
        assert _key("login", "a") == _key("login", "a")
        assert _key("login", "a") != _key("login", "b")

    def test_the_digest_is_truncated_but_long(self) -> None:
        digest = _key("login", "a").removeprefix("rl:login:")

        assert len(digest) == 32
        assert digest == digest.lower()


class TestComposite:
    """10 §6.4 keys login on "IP+account"; the join must not be forgeable."""

    def test_the_parts_cannot_be_re_split_into_a_collision(self) -> None:
        # The reason a plain concatenation will not do: these two pairs are
        # different keys and must not produce the same string.
        assert composite("1.2.3.4", "5678") != composite("1.2.3.45", "678")

    def test_order_matters(self) -> None:
        assert composite("a", "b") != composite("b", "a")

    def test_it_joins_with_a_character_that_cannot_appear_in_the_parts(self) -> None:
        assert "\x1f" in composite("a", "b")


class TestCounting:
    """The window, counted. Identifiers are random per test so nothing leaks
    between runs through Redis or through a shared LocMemCache instance."""

    def test_a_scope_allows_exactly_its_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(django_settings, "RATE_LIMITS", _limited(3))
        who = uuid4().hex

        seen = [consume("probe", who) for _ in range(4)]

        assert [d.allowed for d in seen] == [True, True, True, False]
        assert [d.remaining for d in seen] == [2, 1, 0, 0]
        assert all(d.limit == 3 for d in seen)

    def test_the_refusal_says_how_long_to_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """04 §1 puts this in the ``Retry-After`` header, so it must be usable.

        Bounded rather than exact: with a real Redis the value is the remaining
        TTL, and this module runs on locmem, which has none.
        """
        monkeypatch.setattr(django_settings, "RATE_LIMITS", _limited(1, window=900))
        who = uuid4().hex

        consume("probe", who)
        refused = consume("probe", who)

        assert refused.allowed is False
        assert 0 < refused.retry_after <= 900

    def test_an_allowed_decision_asks_for_no_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(django_settings, "RATE_LIMITS", _limited(3))

        assert consume("probe", uuid4().hex).retry_after == 0

    def test_the_counters_are_separate_per_identifier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(django_settings, "RATE_LIMITS", _limited(1))
        one, two = uuid4().hex, uuid4().hex

        assert consume("probe", one).allowed is True
        assert consume("probe", one).allowed is False
        # One member exhausting their allowance must not spend anyone else's.
        assert consume("probe", two).allowed is True

    def test_the_counters_are_separate_per_scope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A scope is not a per-user budget shared across actions.

        Deliberate: five wrong PINs should not consume the OTP allowance, or an
        attacker could exhaust a member's ability to receive a code by guessing
        PINs.
        """
        monkeypatch.setattr(
            django_settings,
            "RATE_LIMITS",
            {
                "probe_a": {"limit": 1, "window_seconds": 900},
                "probe_b": {"limit": 1, "window_seconds": 900},
            },
        )
        who = uuid4().hex

        assert consume("probe_a", who).allowed is True
        assert consume("probe_a", who).allowed is False
        assert consume("probe_b", who).allowed is True

    def test_the_two_otp_windows_are_independent(self) -> None:
        """SEC-OTP-004's daily cap and its 15-minute cap are separate scopes.

        Asserted because composing them is the call site's job, and a service
        that checked only one of them would leave either SMS spend or burst rate
        unbounded while looking correct.
        """
        who = uuid4().hex
        for _ in range(3):
            assert consume("otp_issue", who).allowed is True
        assert consume("otp_issue", who).allowed is False

        # The daily scope has seen the same three attempts and is nowhere near
        # its own ceiling — which is the point: it is a different bound.
        assert consume("otp_issue_daily", who).allowed is True

    def test_reset_restores_the_allowance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A member who gets it right is not throttled by their earlier misses."""
        monkeypatch.setattr(django_settings, "RATE_LIMITS", _limited(1))
        who = uuid4().hex
        consume("probe", who)
        assert consume("probe", who).allowed is False

        reset("probe", who)

        assert consume("probe", who).allowed is True

    def test_reset_leaves_other_identifiers_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(django_settings, "RATE_LIMITS", _limited(1))
        one, two = uuid4().hex, uuid4().hex
        consume("probe", one)
        consume("probe", two)

        reset("probe", one)

        assert consume("probe", one).allowed is True
        assert consume("probe", two).allowed is False


class _BrokenCache:
    """A cache whose Redis is unreachable."""

    def add(self, key: str, value: int, timeout: int) -> bool:
        raise ConnectionError("redis is down")

    def incr(self, key: str) -> int:
        raise ConnectionError("redis is down")

    def delete(self, key: str) -> bool:
        raise ConnectionError("redis is down")


class _BoundaryRacingCache:
    """Forces the one race ``consume`` handles: the key exists, then it does not.

    ``incr`` raises ``ValueError`` for a key that is not there, which is the
    documented behaviour of every Django cache backend and the only way that call
    fails. Reproduced here because the real race needs two requests to land in
    the same microsecond at a window boundary, and an untested recovery branch is
    an untested recovery branch whether or not it is hard to reach.
    """

    def __init__(self) -> None:
        self.adds = 0

    def add(self, key: str, value: int, timeout: int) -> bool:
        self.adds += 1
        return self.adds > 1  # reports the key present, then absent

    def incr(self, key: str) -> int:
        raise ValueError("key does not exist")

    def delete(self, key: str) -> bool:
        return True


class TestWhenTheCacheIsUnavailable:
    """10 §6.4's limiter is a control; a control that silently stops controlling
    is worse than one that stops the request. See P1-16."""

    def test_the_default_is_to_refuse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(django_settings, "RATE_LIMITS", _limited(3))
        monkeypatch.setattr(ratelimit, "cache", _BrokenCache())

        with pytest.raises(RateLimiterUnavailable) as refused:
            consume("probe", uuid4().hex)

        assert refused.value.scope == "probe"

    def test_a_scope_marked_allow_lets_the_request_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``financial_write`` is the scope that must not stop the money path."""
        monkeypatch.setattr(ratelimit, "cache", _BrokenCache())

        decision = consume("financial_write", uuid4().hex)

        assert decision.allowed is True
        assert decision.limit == 60

    def test_the_unavailable_scopes_are_the_shipped_ones(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two fail-open scopes, named — and read from the real settings.

        This is the test that would fail if ``on_unavailable`` were widened to a
        third scope during an incident, which is exactly when a fail-open is
        tempting.
        """
        monkeypatch.setattr(ratelimit, "cache", _BrokenCache())

        for scope in ("financial_write", "anonymous_read"):
            assert consume(scope, uuid4().hex).allowed is True

        for scope in (
            "login",
            "otp_issue",
            "otp_verify",
            "pin_entry",
            "password_reset",
            "bulk_send",
        ):
            with pytest.raises(RateLimiterUnavailable):
                consume(scope, uuid4().hex)

    def test_it_is_not_confused_with_a_plain_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The two must be distinguishable to a caller and to an operator.

        A refusal is a client that hit its ceiling and gets a 429 with a
        ``Retry-After``. This is the platform's own control being down, which is
        a P1 in 12's alerting. Collapsing them into one refusal would make an
        outage indistinguishable from an attack in the logs — so ``consume``
        raises rather than returning ``Decision(allowed=False)``.
        """
        monkeypatch.setattr(django_settings, "RATE_LIMITS", _limited(1))
        monkeypatch.setattr(ratelimit, "cache", _BrokenCache())

        with pytest.raises(RateLimiterUnavailable) as refused:
            consume("probe", uuid4().hex)

        assert "Redis" in str(refused.value)
        # A refusal would have carried the scope's ceiling; this carries nothing
        # a caller could mistake for a normal answer.
        assert not hasattr(refused.value, "remaining")

    def test_the_window_boundary_race_opens_a_new_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(django_settings, "RATE_LIMITS", _limited(10))
        racing = _BoundaryRacingCache()
        monkeypatch.setattr(ratelimit, "cache", racing)

        decision = consume("probe", uuid4().hex)

        assert decision.allowed is True
        assert decision.remaining == 9
        # Both attempts were made: the second `add` is the recovery, and a
        # version that only tried once would either raise or let the request
        # through without counting it.
        assert racing.adds == 2

    def test_a_permanently_broken_counter_falls_into_the_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the retry also fails, the scope's policy decides — not an exception
        escaping as a 500.

        A 500 tells the client nothing, is logged as a bug rather than as an
        unavailable dependency, and bypasses the ``on_unavailable`` decision that
        someone made on purpose.
        """
        monkeypatch.setattr(
            django_settings,
            "RATE_LIMITS",
            {
                "probe_a": {"limit": 3, "window_seconds": 900},
                "probe_allow": {
                    "limit": 3,
                    "window_seconds": 900,
                    "on_unavailable": "allow",
                },
            },
        )
        monkeypatch.setattr(ratelimit, "cache", _BrokenCache())

        with pytest.raises(RateLimiterUnavailable):
            consume("probe_a", uuid4().hex)
        assert consume("probe_allow", uuid4().hex).allowed is True


class TestLimitDataclass:
    def test_the_default_is_to_deny(self) -> None:
        assert Limit(limit=1, window_seconds=60).on_unavailable == "deny"
