"""The lockout schedule, as a property — SEC-LOCK-001, P1-20.

Marked ``unit``: ``backoff()`` is a pure function of the counter and the
settings. Asserting it here rather than through the service is what makes it
possible to test *escalates with repetition* as a property — monotonic, doubling,
bounded — instead of as three examples chosen to pass.

The settings tests are guards. Each one fails when the rule it protects is
broken, and a change to any of these numbers is a change to a security control:
it belongs in 10 first, and in this file second.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services import lockout
from apps.accounts.services.lockout import LockState, backoff

pytestmark = pytest.mark.unit


def _cap() -> timedelta:
    return timedelta(seconds=django_settings.LOCKOUT["max_seconds"])


class TestTheSchedule:
    def test_nothing_locks_before_the_threshold(self) -> None:
        for attempts in range(django_settings.LOCKOUT["threshold"]):
            assert backoff(attempts) == timedelta(0), f"{attempts} attempt(s) locked"

    def test_the_threshold_failure_locks_for_the_base_window(self) -> None:
        assert backoff(django_settings.LOCKOUT["threshold"]) == timedelta(
            seconds=django_settings.LOCKOUT["base_seconds"]
        )

    def test_each_further_failure_doubles_it_until_the_ceiling(self) -> None:
        """Both halves of the escalation, in one pass.

        Below the ceiling the window must double exactly; at and above it, any
        further failure must stop making it longer. Written as a loop over the
        settings rather than as a list of expected values, so that changing the
        base or the ceiling changes the test's expectations with it — a hardcoded
        list would fail as "the test is out of date" and invite someone to
        update the list without looking at the schedule.
        """
        threshold = django_settings.LOCKOUT["threshold"]
        ceiling = _cap()

        for attempts in range(threshold, threshold + 16):
            window = backoff(attempts)
            longer = backoff(attempts + 1)

            assert longer >= window, f"{attempts} attempts: the window shrank"
            if window * 2 <= ceiling:
                assert longer == window * 2, f"{attempts} attempts: did not double"
            else:
                assert longer == ceiling, f"{attempts} attempts: exceeded the ceiling"

    def test_the_window_never_exceeds_the_ceiling(self) -> None:
        ceiling = _cap()
        for attempts in range(0, 200):
            assert backoff(attempts) <= ceiling

    def test_a_saturated_counter_costs_nothing_to_evaluate(self) -> None:
        """The ceiling bounds the *result*, not the arithmetic that produces it.

        ``base * 2 ** excess`` builds the large integer before ``min()`` sees it,
        and the counter comes from a database column. A row restored with a
        nonsense value must not turn every authentication for that account into
        an allocation of a very large integer — so the exponent is capped too,
        and the observable consequence is that the answer is still the ceiling.
        """
        assert backoff(10**9) == _cap()

    def test_the_schedule_is_read_from_settings_on_every_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not cached at import.

        The entire reason these numbers are settings is 10 §6.4's "tightened in
        response to an incident without a deploy". A schedule read once and held
        would satisfy every test above and fail that requirement.
        """
        monkeypatch.setattr(
            django_settings,
            "LOCKOUT",
            {"threshold": 2, "base_seconds": 5, "max_seconds": 20},
        )

        assert backoff(2) == timedelta(seconds=5)
        assert backoff(3) == timedelta(seconds=10)
        assert backoff(4) == timedelta(seconds=20)


class TestConfiguration:
    """A schedule that is absent or misspelled must fail loudly, not default open."""

    def test_a_missing_setting_names_the_requirement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delattr(django_settings, "LOCKOUT")

        with pytest.raises(ImproperlyConfigured) as refused:
            backoff(5)

        # Named so the person reading the traceback learns which requirement is
        # not being enforced, rather than only that a key is missing.
        assert "SEC-LOCK-001" in str(refused.value)

    def test_a_setting_that_is_not_a_mapping_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(django_settings, "LOCKOUT", [("threshold", 5)])

        with pytest.raises(ImproperlyConfigured) as refused:
            backoff(5)

        assert "mapping" in str(refused.value)

    @pytest.mark.parametrize(
        "malformed",
        [
            {},  # nothing at all
            {"threshold": 5, "base_seconds": 60},  # no ceiling
            {"threshold": 5, "max_seconds": 900},  # no base
            {"base_seconds": 60, "max_seconds": 900},  # no threshold
            {"threshold": "five", "base_seconds": 60, "max_seconds": 900},
            {"threshold": 5, "base_seconds": "a minute", "max_seconds": 900},
            {"threshold": None, "base_seconds": 60, "max_seconds": 900},
            {"threshold": 0, "base_seconds": 60, "max_seconds": 900},
            {"threshold": -5, "base_seconds": 60, "max_seconds": 900},
            # A threshold of zero would lock every account on its first
            # request; a float (from a YAML `60.0`) would be silently truncated.
            {"threshold": 5.0, "base_seconds": 60, "max_seconds": 900},
            # `bool` is a subclass of `int`, so this would otherwise be accepted
            # as a threshold of 1 — the second wrong password locks the account.
            {"threshold": True, "base_seconds": 60, "max_seconds": 900},
        ],
    )
    def test_a_malformed_schedule_raises(
        self, monkeypatch: pytest.MonkeyPatch, malformed: dict[str, Any]
    ) -> None:
        monkeypatch.setattr(django_settings, "LOCKOUT", malformed)

        with pytest.raises(ImproperlyConfigured):
            backoff(5)

    def test_a_ceiling_below_the_base_window_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ceiling would cut the first lockout short — not a schedule
        anybody chose, and almost certainly a units mistake (seconds for
        minutes) rather than an intention."""
        monkeypatch.setattr(
            django_settings,
            "LOCKOUT",
            {"threshold": 5, "base_seconds": 600, "max_seconds": 60},
        )

        with pytest.raises(ImproperlyConfigured) as refused:
            backoff(5)

        assert "max_seconds" in str(refused.value)

    def test_a_flat_schedule_is_permitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``max_seconds == base_seconds`` is legal: no escalation, one window.

        Permitted on purpose. It is what an incident response reaches for when
        the escalation itself is being abused, and it is still a lockout — the
        requirement's *escalation* is what this gives up, not its existence.
        """
        monkeypatch.setattr(
            django_settings,
            "LOCKOUT",
            {"threshold": 3, "base_seconds": 60, "max_seconds": 60},
        )

        assert backoff(3) == timedelta(seconds=60)
        assert backoff(9) == timedelta(seconds=60)


class TestTheShippedValues:
    """The numbers themselves, against the documents that decide them."""

    def test_the_shipped_schedule_is_the_documented_default(self) -> None:
        assert django_settings.LOCKOUT == {
            # SEC-LOCK-001: "N consecutive failed logins (default 5)".
            "threshold": 5,
            # The shape of the escalation is not in 10 at all — it says only
            # "escalates with repetition" — so these two are P1-20's decision.
            "base_seconds": 60,
            "max_seconds": 15 * 60,
        }

    def test_the_threshold_is_not_so_low_that_a_mistake_locks_an_account(self) -> None:
        """A threshold of one or two turns an ordinary typo into a lockout.

        SEC-LOCK-001's default is five; a deployment may tighten it, and the
        value here is the floor rather than the ceiling of what is reasonable.
        """
        assert django_settings.LOCKOUT["threshold"] >= 3

    def test_the_ceiling_is_a_wait_a_member_can_sit_out(self) -> None:
        """The bound that keeps the control from becoming an attack.

        A lockout window is also a window in which anyone who knows a member's
        phone number can keep them out. A day would be an effective denial of
        service on a real person; half an hour is the most that is defensible,
        and the escalation is what makes a short ceiling sufficient.
        """
        assert django_settings.LOCKOUT["max_seconds"] <= 30 * 60


class TestLockState:
    def test_a_closed_window_has_no_wait(self) -> None:
        now = timezone.now()
        closed = LockState(locked=False, failed_attempts=0, locked_until=None)

        assert closed.remaining(now) == timedelta(0)

    def test_a_deadline_in_the_past_has_no_wait(self) -> None:
        """``remaining()`` is total: it does not require the field to agree with
        it. A stale deadline reaching a ``Retry-After`` would tell a member to
        wait for a lockout that is already over."""
        now = timezone.now()
        stale = LockState(
            locked=True, failed_attempts=5, locked_until=now - timedelta(seconds=1)
        )

        assert stale.remaining(now) == timedelta(0)

    def test_the_wait_is_the_distance_to_the_deadline(self) -> None:
        now = timezone.now()
        deadline = now + timedelta(seconds=90)
        open_window = LockState(locked=True, failed_attempts=5, locked_until=deadline)

        assert open_window.remaining(now) == timedelta(seconds=90)

    def test_state_hides_a_deadline_that_has_passed(self) -> None:
        """The masking described on ``LockState``.

        An unsaved instance is enough: this is arithmetic over the two columns,
        and the module's contract is about what it *reports*, not about where the
        row came from.
        """
        now = timezone.now()
        user = User(locked_until=now - timedelta(seconds=1), failed_login_attempts=7)

        assert lockout.state(user, now=now) == LockState(
            locked=False, failed_attempts=7, locked_until=None
        )
