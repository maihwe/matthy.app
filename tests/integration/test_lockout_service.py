"""Lockout behaviour against real PostgreSQL — SEC-LOCK-001, NFR-SEC-002, P1-21.

Marked ``integration`` for the reason the other service suites are: the counter
is incremented under ``SELECT ... FOR UPDATE``, and the lock is the control. On
SQLite these tests would pass while the serialisation they describe did not
exist. The contention itself is asserted separately, in
``tests/concurrency/test_lockout_contention.py`` — one thread on one connection
cannot show a lost update, however many failures it records.

Marked ``security`` because every assertion is an obligation from 10 §2 or §3.

Durations are asserted exactly where they can be — ``record_failure`` computes
``stamp + window``, so passing the same ``stamp`` it was given makes the
comparison exact rather than "about a minute". Where the *shipped* schedule's
numbers matter the shipped settings are used; where only the shape matters a
small schedule is installed, so the escalation is observable without waiting out
a fifteen-minute ceiling.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest
from django.conf import settings as django_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services import lockout
from apps.accounts.services.lockout import LockState, clear, record_failure, state
from tests.factories import UserFactory

pytestmark = [pytest.mark.integration, pytest.mark.django_db, pytest.mark.security]

SMALL_SCHEDULE = {"threshold": 3, "base_seconds": 2, "max_seconds": 8}
"""Threshold 3, doubling from 2s and capped at 8s — the shipped *shape* with
numbers small enough to assert exactly. The shipped numbers are asserted in
``tests/unit/test_lockout_backoff.py`` and are not what these tests are about.
"""


def _fail(user: User, times: int = 1) -> LockState:
    """Record ``times`` failures and return the last result."""
    if times < 1:
        raise ValueError("_fail() records at least one failure.")

    result = record_failure(user)
    for _ in range(times - 1):
        result = record_failure(user)
    return result


def _expire(user: User, *, seconds: int = 1) -> None:
    """Push the deadline into the past, as the passage of time would.

    Written as an ``UPDATE`` rather than by waiting: a suite that sleeps out its
    own lockouts is a suite that gets skipped.
    """
    User.objects.filter(pk=user.pk).update(
        locked_until=timezone.now() - timedelta(seconds=seconds)
    )
    user.refresh_from_db()


def _records(caplog: pytest.LogCaptureFixture, message: str) -> list[logging.LogRecord]:
    """Captured records with this message.

    Filtered by message because the module logger emits more than one event, and
    a test that took "the last record" would be asserting about whichever came
    last. Read with ``getattr`` because ``LogRecord`` does not declare the keys a
    caller passed in ``extra``, and mypy strict checks this file.
    """
    return [record for record in caplog.records if record.getMessage() == message]


class TestTheCounter:
    def test_failures_below_the_threshold_do_not_lock(self) -> None:
        user = UserFactory()
        threshold = django_settings.LOCKOUT["threshold"]

        result = _fail(user, threshold - 1)

        assert result.locked is False
        assert result.failed_attempts == threshold - 1
        assert result.locked_until is None

    def test_the_threshold_failure_locks_the_account(self) -> None:
        """SEC-LOCK-001's "N consecutive failed logins ... locks the account"."""
        user = UserFactory()

        result = _fail(user, django_settings.LOCKOUT["threshold"])

        assert result.locked is True
        assert result.locked_until is not None
        assert result.remaining(timezone.now()) > timedelta(0)

    def test_the_counter_is_persisted_not_just_returned(self) -> None:
        user = UserFactory()

        _fail(user, 3)

        stored = User.objects.get(pk=user.pk)
        assert stored.failed_login_attempts == 3

    def test_the_caller_instance_is_brought_up_to_date(self) -> None:
        """The object the caller passed is the one it will render and log from.

        A stale instance is how a login response ends up reporting the state
        before the failure it is reporting — and, worse, how a caller that
        checked ``user.is_locked`` after the call would see the old answer.
        """
        user = UserFactory()

        record_failure(user)

        assert user.failed_login_attempts == 1

    def test_a_locked_account_reports_its_deadline(self) -> None:
        user = UserFactory()

        result = _fail(user, django_settings.LOCKOUT["threshold"])

        reported = state(user)

        assert reported.locked is True
        assert reported.locked_until == result.locked_until


class TestEscalation:
    """SEC-LOCK-001's "backoff period that escalates with repetition"."""

    def test_the_first_lockout_is_the_base_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(django_settings, "LOCKOUT", SMALL_SCHEDULE)
        user = UserFactory()
        _fail(user, SMALL_SCHEDULE["threshold"] - 1)

        now = timezone.now()
        result = record_failure(user, now=now)

        assert result.locked_until == now + timedelta(seconds=SMALL_SCHEDULE["base_seconds"])

    def test_a_failure_after_the_window_doubles_the_next_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(django_settings, "LOCKOUT", SMALL_SCHEDULE)
        user = UserFactory()
        _fail(user, SMALL_SCHEDULE["threshold"])
        _expire(user)

        now = timezone.now()
        result = record_failure(user, now=now)

        assert result.locked_until == now + timedelta(seconds=2 * SMALL_SCHEDULE["base_seconds"])

    def test_the_window_stops_growing_at_the_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The escalation is bounded, and that bound is the point (P1-20).

        Each pass expires the previous window and fails once more, so this walks
        the schedule the way an attacker who returns every window would. The
        assertion is on the last two, not on a specific one: what matters is that
        it stops growing, not the step it stops at.
        """
        monkeypatch.setattr(django_settings, "LOCKOUT", SMALL_SCHEDULE)
        user = UserFactory()

        windows = []
        for _ in range(8):
            now = timezone.now()
            result = record_failure(user, now=now)
            if result.locked_until is not None:
                windows.append(result.locked_until - now)
            _expire(user)

        cap = timedelta(seconds=SMALL_SCHEDULE["max_seconds"])
        assert windows[-1] == cap
        assert windows[-2] == cap

    def test_the_counter_saturates_without_overflowing_the_column(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A counter at PostgreSQL's smallint limit must still lock, not 500.

        Reachable only by a corrupt or restored row — the application cannot get
        here — and the outcome without the ceiling would be the worst of both: an
        INSERT that raises inside the failure path, so an account whose password
        is being guessed is the one account that never gets locked.
        """
        monkeypatch.setattr(django_settings, "LOCKOUT", SMALL_SCHEDULE)
        user = UserFactory()
        User.objects.filter(pk=user.pk).update(failed_login_attempts=32_767)
        user.refresh_from_db()

        now = timezone.now()
        result = record_failure(user, now=now)

        assert result.locked is True
        assert result.locked_until == now + timedelta(seconds=SMALL_SCHEDULE["max_seconds"])
        assert User.objects.get(pk=user.pk).failed_login_attempts == 32_767


class TestTheWindowIsNotExtendedFromInside:
    """P1-21 — the decision that keeps lockout from being a DoS on the member."""

    def test_an_attempt_inside_the_window_changes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(django_settings, "LOCKOUT", SMALL_SCHEDULE)
        user = UserFactory()
        locked = _fail(user, SMALL_SCHEDULE["threshold"])
        deadline, attempts = locked.locked_until, locked.failed_attempts

        inside = deadline - timedelta(seconds=1)
        result = record_failure(user, now=inside)

        assert result.locked_until == deadline
        assert result.failed_attempts == attempts

    def test_hammering_inside_one_window_never_moves_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The attack this decision closes.

        A phone number is public — it is how a collector finds a member — so if
        attempts made while locked counted, anyone could hold a stranger's account
        closed indefinitely for the price of one request per window. Twenty
        attempts spread across a single open window must leave the deadline
        exactly where the first one put it.
        """
        monkeypatch.setattr(django_settings, "LOCKOUT", SMALL_SCHEDULE)
        user = UserFactory()
        locked = _fail(user, SMALL_SCHEDULE["threshold"])
        deadline = locked.locked_until

        result = locked
        start = deadline - timedelta(seconds=SMALL_SCHEDULE["max_seconds"])
        for step in range(20):
            result = record_failure(user, now=start + timedelta(milliseconds=step * 10))

        assert result.locked_until == deadline
        assert User.objects.get(pk=user.pk).failed_login_attempts == locked.failed_attempts

    def test_the_window_does_not_extend_itself_on_a_retry_after_it_closes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once the window closes, a further failure earns the *next* window —
        which is longer, and still bounded (see ``TestEscalation``)."""
        monkeypatch.setattr(django_settings, "LOCKOUT", SMALL_SCHEDULE)
        user = UserFactory()
        first = _fail(user, SMALL_SCHEDULE["threshold"])
        _expire(user)

        second = record_failure(user)

        assert second.locked_until > first.locked_until


class TestClearing:
    def test_a_successful_authentication_resets_the_streak(self) -> None:
        """SEC-LOCK-001 locks on *consecutive* failures.

        Without this, a member who mistypes their password four times a month
        would be locked out by the fourth month — and the lockout would look
        arbitrary to them, because nothing they did that day was unusual.
        """
        user = UserFactory()
        _fail(user, django_settings.LOCKOUT["threshold"])
        _expire(user)

        clear(user)

        assert state(user).failed_attempts == 0
        assert User.objects.get(pk=user.pk).failed_login_attempts == 0

    def test_clearing_ends_an_open_lockout(self) -> None:
        """Reachable when the password is right but the window is open only if a
        caller clears before checking — so this is a guard on the *service*, not
        a description of the login path. It matters because a password reset
        during a lockout is the ordinary way a member gets back in."""
        user = UserFactory()
        _fail(user, django_settings.LOCKOUT["threshold"])

        clear(user)

        assert state(user).locked is False
        assert User.objects.get(pk=user.pk).locked_until is None

    def test_a_successful_login_costs_no_write(
        self, django_assert_num_queries: Any
    ) -> None:
        """The ordinary case: the first attempt is the right password.

        Clearing unconditionally would put an ``UPDATE users`` on every
        successful sign-in for the life of the platform, to write zeroes that are
        already there.
        """
        user = UserFactory()

        with django_assert_num_queries(0):
            clear(user)

    def test_the_streak_starts_again_from_one(self) -> None:
        """Clearing resets the *count*, so the escalation it earned is forgotten.

        That is the intent — a member who has proved they know the password
        starts over — and it is worth stating because the alternative (remembering
        the history to escalate across sessions) is what SEC-LOCK-002's per-IP
        half exists to cover, and it is missing (C3).
        """
        user = UserFactory()
        _fail(user, django_settings.LOCKOUT["threshold"])
        clear(user)

        result = record_failure(user)

        assert result.failed_attempts == 1
        assert result.locked is False


class TestLogging:
    def test_the_lockout_is_logged_with_the_window(self, caplog: pytest.LogCaptureFixture) -> None:
        """SEC-AUD-005 wants this alertable. There is no security sink yet (C5),
        so this record is the interim mechanism, and therefore testable."""
        user = UserFactory()

        with caplog.at_level(logging.WARNING, logger="apps.accounts.services.lockout"):
            _fail(user, django_settings.LOCKOUT["threshold"])

        events = _records(caplog, "account.locked")
        assert len(events) == 1
        assert getattr(events[0], "lockout_seconds") == django_settings.LOCKOUT["base_seconds"]
        assert getattr(events[0], "user_public_id") == str(user.public_id)

    def test_ordinary_failures_are_not_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Every mistyped password in the country is not an alert."""
        user = UserFactory()

        with caplog.at_level(logging.WARNING, logger="apps.accounts.services.lockout"):
            _fail(user, django_settings.LOCKOUT["threshold"] - 1)

        assert not _records(caplog, "account.locked")

    def test_the_attempts_inside_an_open_window_are_not_logged_again(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(django_settings, "LOCKOUT", SMALL_SCHEDULE)
        user = UserFactory()
        locked = _fail(user, SMALL_SCHEDULE["threshold"])
        inside = locked.locked_until - timedelta(seconds=1)

        with caplog.at_level(logging.WARNING, logger="apps.accounts.services.lockout"):
            for _ in range(5):
                record_failure(user, now=inside)

        assert not _records(caplog, "account.locked")

    def test_the_log_does_not_carry_the_phone_number(self, caplog: pytest.LogCaptureFixture) -> None:
        """S1 and 10 §5.2: a log line is a place with no access control.

        The record identifies the account by ``public_id`` — which is what an
        investigation needs — and the phone number, which is the credential's
        username, stays out of it.
        """
        user = UserFactory()

        with caplog.at_level(logging.WARNING, logger="apps.accounts.services.lockout"):
            _fail(user, django_settings.LOCKOUT["threshold"])

        event = _records(caplog, "account.locked")[0]
        written = event.getMessage() + str(event.__dict__)
        assert str(user.phone) not in written


class TestNothingIsDecidedForTheCaller:
    """SEC-AUTH-005 — the module returns state and refuses to have an opinion."""

    def test_no_exception_is_exported(self) -> None:
        """A distinct ``AccountLocked`` error would be the oracle SEC-AUTH-005
        exists to prevent. The login path collapses a locked account, an unknown
        number and a wrong password into one answer, so the lockout has to arrive
        as state that the caller deliberately discards, not as something the
        caller can raise a distinct response from.

        The guard is on the module's public surface, so adding one fails here
        before it reaches anything that would return it.
        """
        exported = [getattr(lockout, name) for name in lockout.__all__]

        assert exported
        assert not [
            obj
            for obj in exported
            if isinstance(obj, type) and issubclass(obj, BaseException)
        ]

    def test_a_locked_account_is_refused_without_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal is a return value, and calling it again is safe."""
        monkeypatch.setattr(django_settings, "LOCKOUT", SMALL_SCHEDULE)
        user = UserFactory()
        _fail(user, SMALL_SCHEDULE["threshold"])

        result = record_failure(user)

        assert isinstance(result, LockState)
        assert result.locked is True
