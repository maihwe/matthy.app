"""Credential-failure backoff — NFR-SEC-002, SEC-LOCK-001.

**This module owns the per-account half of SEC-LOCK-002 and nothing else.** The
requirement is that lockout is per-account *and* per-source-IP. The account half
is expressible in the schema, because ``users`` has ``failed_login_attempts`` and
``locked_until``. The IP half is not: it is a counter over an address, not over a
row, and there is no table for it. That is **C3** — 03 has no column, and 10 §6.4
defines no scope for it either, so implementing it here would mean inventing
both. It is left out deliberately and named, rather than half-done in a way that
reads as complete.

**The escalating window is derived, not stored.** SEC-LOCK-001 asks for a backoff
"that escalates with repetition", and ``failed_login_attempts`` already *is* the
count of repetitions — it is reset only by a successful authentication. So the
window is a pure function of the counter, and no column is added. That matters
beyond tidiness: a stored ``lockout_count`` would be a second source of truth for
"how many failures has this account had", and the two would disagree the first
time one path forgot to increment it.

**A failure during an open window does not extend it.** This is the one decision
here that is not forced by the requirement, so it is written down as **P1-21**.
The alternative — counting attempts made while locked, as if the account were not
locked — lets anyone who knows a phone number keep that account closed forever by
sending one request per window. A phone number is not a secret; it is how a
collector finds a member. So an attempt inside the window is refused, and leaves
the deadline where it was. The escalation still happens, because an attacker who
returns after each window closes produces a fresh failure each time, and the
schedule is what bounds them.

**Nothing here raises a domain error, and nothing here decides a response.** The
caller owns the message: SEC-AUTH-005 collapses a locked account, an unknown
number and a wrong password into one answer, so a distinct exception from this
module would be the oracle that requirement exists to prevent. Return values
instead, and the caller decides what the member is told.

**Nothing here writes an audit record.** SEC-AUD-005 wants a lockout to be
alertable and SEC-AUD-001 wants security events "distinct from business audit",
but there is no security sink in the design yet — **C5**. A log line is the
interim mechanism, and it is what the tests assert; see
``sessions.rotate()``, which made the same choice for the same reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User

__all__ = [
    "LockState",
    "backoff",
    "clear",
    "record_failure",
    "state",
]

logger = logging.getLogger(__name__)

_SETTING = "LOCKOUT"
_REQUIRED_KEYS = ("threshold", "base_seconds", "max_seconds")

_MAX_EXPONENT = 32
"""Bounds the doubling's exponent before the cap is applied.

The cap above already bounds the *result*, so this looks redundant — it is not.
``base * 2 ** excess`` computes the large integer before ``min()`` sees it, and
``excess`` comes from a database column. A corrupt or deliberately saturated
counter would otherwise turn every authentication for that account into an
allocation of a very large integer.
"""

_COUNTER_CEILING = 30_000
"""Where ``failed_login_attempts`` stops growing.

It is a ``SmallIntegerField``: PostgreSQL raises on a value past 32767 rather
than wrapping. An overflow here would not be a quiet miscount — it would be an
account that cannot authenticate at all, with every attempt returning a 500 from
inside the failure path meant to protect it. The ceiling is below that limit and
above the point where the backoff schedule has already saturated, so nothing that
reads the counter can tell the difference.
"""


@dataclass(frozen=True, slots=True)
class LockState:
    """What the account's lockout columns say, as of one instant.

    ``locked_until`` is reported as ``None`` when the window is closed, even if
    the column holds a timestamp in the past. A caller rendering "locked until
    14:02" from a stale column would be telling a member they are locked out when
    the next attempt would succeed.
    """

    locked: bool
    failed_attempts: int
    locked_until: datetime | None

    def remaining(self, now: datetime) -> timedelta:
        """How long the member must wait. Zero when the window is closed.

        This is what a ``Retry-After`` needs, and it is computed from the same
        deadline the lock is evaluated against, so the header and the refusal
        cannot disagree.
        """
        if self.locked_until is None or self.locked_until <= now:
            return timedelta(0)
        return self.locked_until - now


def _configured() -> tuple[int, int, int]:
    """Read and validate ``settings.LOCKOUT``. Returns (threshold, base, cap).

    Raises rather than defaulting, for P1-15's reason: a lockout control that
    falls back to "unlimited" when a setting is misspelled is a control that
    stopped working without anyone being told.
    """
    table = getattr(settings, _SETTING, None)
    if not isinstance(table, dict):
        raise ImproperlyConfigured(
            f"settings.{_SETTING} is missing or is not a mapping. "
            "SEC-LOCK-001's backoff cannot be applied without it."
        )

    values: dict[str, int] = {}
    for key in _REQUIRED_KEYS:
        value = table.get(key)
        # `bool` is excluded explicitly. It is a subclass of `int`, so
        # `threshold: true` would otherwise be accepted as a threshold of 1 —
        # a YAML/JSON typo that turns the second wrong password into a lockout.
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ImproperlyConfigured(
                f"settings.{_SETTING}[{key!r}] must be a positive integer, "
                f"got {value!r}."
            )
        values[key] = value

    if values["max_seconds"] < values["base_seconds"]:
        raise ImproperlyConfigured(
            f"settings.{_SETTING}['max_seconds'] ({values['max_seconds']}) is "
            f"below 'base_seconds' ({values['base_seconds']}): the ceiling would "
            "cut the first lockout short, which is a schedule nobody chose."
        )

    return values["threshold"], values["base_seconds"], values["max_seconds"]


def backoff(failed_attempts: int) -> timedelta:
    """The lockout window a given failure count earns. Zero below the threshold.

    Pure, so the schedule can be asserted without a database and without a clock
    — which is the only way to test "escalates with repetition" as a property
    rather than as three examples of it.
    """
    threshold, base_seconds, max_seconds = _configured()

    if failed_attempts < threshold:
        return timedelta(0)

    excess = min(failed_attempts - threshold, _MAX_EXPONENT)
    return timedelta(seconds=min(base_seconds * (1 << excess), max_seconds))


def state(user: User, *, now: datetime | None = None) -> LockState:
    """Read an account's lockout state without writing anything."""
    stamp = now or timezone.now()
    locked = user.is_locked_at(stamp)
    return LockState(
        locked=locked,
        failed_attempts=user.failed_login_attempts,
        locked_until=user.locked_until if locked else None,
    )


def record_failure(user: User, *, now: datetime | None = None) -> LockState:
    """Count one failed credential check, and lock the account if it is due.

    The row is re-read under ``SELECT ... FOR UPDATE`` rather than incremented
    with ``F()``: the decision to lock depends on the counter *after* the
    increment, so the read, the arithmetic and the write have to be one atomic
    unit. With ``F()`` alone, two simultaneous failures would both read 4, both
    write 5, and the account would need six failures to lock instead of five —
    in exactly the situation (a burst of attempts) the control exists for.

    The caller's instance is updated in place afterwards, because it is the
    object the caller will log and render from, and leaving it stale is how a
    response ends up describing the state before the failure it is reporting.
    """
    stamp = now or timezone.now()
    window = timedelta(0)

    with transaction.atomic():
        locked = User.objects.select_for_update().get(pk=user.pk)

        # P1-21: an attempt inside an open window is refused and changes
        # nothing. Counting it would let a stranger hold this account closed
        # indefinitely, one request per window.
        if not locked.is_locked_at(stamp):
            # Held at the ceiling rather than clamped to it: a row that somehow
            # sits above the ceiling (restored from a dump, written by hand) must
            # not be *decremented* by a failure, which would be a failure path
            # that silently improves the account's record.
            attempts = (
                locked.failed_login_attempts + 1
                if locked.failed_login_attempts < _COUNTER_CEILING
                else locked.failed_login_attempts
            )
            window = backoff(attempts)
            locked.failed_login_attempts = attempts
            locked.locked_until = stamp + window if window else None
            locked.save(update_fields=["failed_login_attempts", "locked_until"])

    # The caller holds this object and will log and render from it. Leaving it
    # stale is how a response describes the state before the failure it reports.
    user.failed_login_attempts = locked.failed_login_attempts
    user.locked_until = locked.locked_until

    if window:
        # Only when the account actually locked. Logging every failure would put
        # one line in the log for every mistyped password in the country, and the
        # signal worth alerting on is the lockout, not the typo.
        logger.warning(
            "account.locked",
            extra={
                "user_public_id": str(locked.public_id),
                "failed_login_attempts": locked.failed_login_attempts,
                "lockout_seconds": int(window.total_seconds()),
            },
        )

    return state(locked, now=stamp)


def clear(user: User) -> None:
    """Forget the failure streak. Called after a successful authentication.

    SEC-LOCK-001 says *consecutive* failures, so a success is what resets the
    count — not the passage of time. Without this, a member who mistypes their
    password four times a month would be locked out by the fourth month.

    Written as one ``UPDATE`` rather than a read-modify-write in Python: nothing
    here depends on the previous value, so there is no reason to hold a lock for
    it. If this races a concurrent ``record_failure``, one of the two arrives
    last and the outcome is that a single failure is not counted — the benign
    direction, and not worth serialising every successful login to prevent.
    """
    if user.failed_login_attempts == 0 and user.locked_until is None:
        # The common case by a wide margin: most logins are the first attempt.
        # A write here would put an UPDATE on every successful sign-in forever.
        return

    User.objects.filter(pk=user.pk).update(failed_login_attempts=0, locked_until=None)
    user.clear_lockout()
    logger.info("account.lockout_cleared", extra={"user_public_id": str(user.public_id)})
