"""The lockout counter under contention — SEC-LOCK-001, 13 §2's lost update.

``test_lockout_service.py`` names this file in its docstring: the behaviour is
asserted there, and the *contention* is asserted here. The split is not tidiness.
Every other lockout test runs in one thread on one connection, so an
implementation that read the counter, added one in Python and wrote it back
would pass all of them — and would still fail to lock an account after the
configured number of attempts whenever the attempts arrived together, which is
the only situation a lockout exists for.

**What "lost update" costs here, concretely.** SEC-LOCK-001's threshold is the
number of *consecutive failures* that locks an account. With a non-locking
read-modify-write, two simultaneous failures both read 0 and both write 1: the
counter says 1 where the truth is 2, the threshold is reached one failed guess
late, and an attacker whose guesses are parallel gets a free attempt per
collision. `record_failure`'s docstring states the fix — re-read under
``SELECT ... FOR UPDATE`` so the read, the arithmetic and the write are one
atomic unit — and these tests are what hold it to that.

**Why ``transaction=True``, and why the failure is a hang.** With pytest-django's
default wrapping transaction the row is uncommitted, so a second connection
cannot see it and ``SELECT ... FOR UPDATE`` blocks until the wrapper ends, which
is at teardown. The observable symptom is a hung CI job, not a failed test, and a
hang reads as infrastructure rather than as the bug it is. The alternative —
weakening the test to share one connection — removes the contention being
tested, so it is not a trade worth making.

**Nothing here asserts an order.** The threads are unordered by construction, so
every assertion is on a value that must hold regardless of which thread wins:
a total, or the fact that a set of operations is idempotent under contention.
Asserting "the second failure is the one that locks" would be asserting the
scheduler.

**Not covered: ``clear()``.** It is deliberately absent rather than forgotten.
``clear()`` performs no read-modify-write — it is a single ``UPDATE`` whose result
does not depend on the value it replaces — so there is no update for a second
thread to lose. When it does race ``record_failure``, the outcome the docstring
names is "one failure is not counted", which is a property of an interleaving
rather than of the code: a test asserting it would be asserting that a race is
usually benign, which is the kind of test that fails once a month and teaches
nothing when it does.

**None of this file has ever been executed.** No toolchain is installed on the
development machine that produced it (see ``docs/PHASE_1_PLAN.md`` §6), so the
threading, the barrier timeouts and the connection handling below are reasoned
but unverified. The first run is expected to be where the harness assumptions —
not the service ones — need adjusting.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.conf import settings as django_settings
from django.db import connections

from apps.accounts.models import User
from apps.accounts.services import lockout
from tests.factories import UserFactory

pytestmark = [
    pytest.mark.concurrency,
    pytest.mark.django_db(transaction=True),
    pytest.mark.security,
]

_BASE_SECONDS = 30
"""The window the installed schedule grants.

Large on purpose, and it is an anti-flake decision rather than a realistic one.
With a two-second window the assertion "the account is locked" could be evaluated
after the window had already closed on a loaded CI machine, and the test would
fail intermittently for a reason unrelated to what it tests. Thirty seconds is
far longer than the interval between the last write and the assertion, and still
short enough that the test does not wait for it — nothing here sleeps it out.
"""

_BARRIER_TIMEOUT = 15.0
"""How long a worker waits for its peers before giving up.

The barrier is what makes the threads collide rather than merely coexist. Without
a timeout a worker that errored before reaching the barrier would leave the
others waiting forever, converting a one-line failure into a hung job — the
failure mode this whole file is arranged to avoid.
"""

_JOIN_TIMEOUT = 30.0
"""How long the main thread waits on each worker's result.

A *reporting* aid, not a hang guard, and the distinction is worth stating because
the number looks like one. ``ThreadPoolExecutor.__exit__`` calls
``shutdown(wait=True)`` with no timeout of its own, so a worker that is genuinely
stuck blocks the ``with`` block regardless of what this value is. The hang
protection is ``_BARRIER_TIMEOUT``, which every worker is subject to; this value
exists so that a worker which raised reports its own traceback promptly instead
of being folded into a later, less specific failure.
"""


def _schedule(threshold: int) -> dict[str, int]:
    """The shipped schedule's shape with a threshold this test can reach.

    ``base_seconds`` and ``max_seconds`` are equal, so the first failure past the
    threshold already earns the full window and no test has to reason about which
    doubling it landed on.
    """
    return {
        "threshold": threshold,
        "base_seconds": _BASE_SECONDS,
        "max_seconds": _BASE_SECONDS,
    }


def _fail_once(pk: int, barrier: threading.Barrier) -> None:
    """One thread's entire contribution: meet at the barrier, then record a failure.

    The instance is loaded per thread rather than passed in. Two reasons, and the
    first is the one that matters: ``record_failure`` writes the resulting state
    back onto the instance it was given, so handing one object to several threads
    would add a Python-level data race on top of the SQL-level one under test.
    The second is realism — a real request loads its own instance, and the
    lockout's job is to be correct for requests that did not coordinate.
    """
    try:
        barrier.wait(timeout=_BARRIER_TIMEOUT)
        lockout.record_failure(User.objects.get(pk=pk))
    finally:
        # `connections` is thread-local, so this closes this thread's connection
        # and leaves the main thread's alone. Skipped, the connection outlives
        # the thread, and `transaction=True` teardown flushes tables while a
        # worker still holds one — which blocks rather than fails.
        connections.close_all()


def _stamp_failures(pk: int, workers: int) -> None:
    """Run ``workers`` simultaneous failures against one account, or fail trying.

    ``future.result()`` re-raises whatever the worker raised; a bare ``submit``
    would swallow it into the pool and let the test pass on a thread that died.
    """
    barrier = threading.Barrier(workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_fail_once, pk, barrier) for _ in range(workers)]
        for future in futures:
            future.result(timeout=_JOIN_TIMEOUT)


class TestTheCounterIsNotLost:
    def test_two_simultaneous_failures_count_twice_and_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lost update, in its smallest form.

        Threshold 2 makes the corruption visible as a *control failure* rather
        than as a number: a non-locking read-modify-write leaves the counter at 1
        and the account unlocked, so the second guess — the one that should have
        been refused — is answered instead. Asserting only the counter would miss
        that; asserting only the lock would miss the mechanism.
        """
        monkeypatch.setattr(django_settings, "LOCKOUT", _schedule(threshold=2))
        member = UserFactory()

        _stamp_failures(member.pk, workers=2)

        stored = User.objects.get(pk=member.pk)
        assert stored.failed_login_attempts == 2
        assert lockout.state(stored).locked is True

    def test_a_wider_burst_counts_every_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Five threads, threshold 5 — the same property at the size SEC-LOCK-001 names.

        Worth having beside the two-thread case because a partial fix passes that
        one and fails this: serialising the *write* but not the read lets pairs
        of threads lose each other's increments, which two threads cannot show.
        """
        monkeypatch.setattr(django_settings, "LOCKOUT", _schedule(threshold=5))
        member = UserFactory()

        _stamp_failures(member.pk, workers=5)

        stored = User.objects.get(pk=member.pk)
        assert stored.failed_login_attempts == 5
        assert lockout.state(stored).locked is True


class TestAnAlreadyLockedAccountAbsorbsTheBurst:
    def test_concurrent_failures_against_a_locked_account_change_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P1-21 under contention, which is where it is worth the most.

        Sequentially, "an attempt inside an open window neither counts nor moves
        the deadline" is easy. Concurrently it is the same decision made by three
        threads that each have to observe the *other's* write to get it right —
        and an implementation that increments before testing the window turns a
        burst into an escalating lockout, which is the denial of service P1-21
        exists to prevent.

        Threshold 1 makes the expected value exactly 1 whatever order the threads
        run in, so the assertion is a constant rather than a race outcome.
        """
        monkeypatch.setattr(django_settings, "LOCKOUT", _schedule(threshold=1))
        member = UserFactory()

        _stamp_failures(member.pk, workers=3)

        stored = User.objects.get(pk=member.pk)
        assert stored.failed_login_attempts == 1
        assert lockout.state(stored).locked is True
