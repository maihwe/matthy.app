"""Login — FR-AUTH-002, FR-AUTH-013, SEC-AUTH-005, SEC-LOCK-001/002.

Marked ``integration``: the counter the lockout reads lives in a column, the
status branch reads another, and the ``SELECT ... FOR UPDATE`` inside
``lockout.record_failure`` only means anything on a backend that has row locks.

Marked ``security`` for the whole file, because nearly every assertion here is a
security obligation rather than a behaviour: the collapse of six different
failures onto one answer, the ordering that keeps the status out of an
unauthenticated response, and the absence of an identifier from a log record.

**These tests need Redis.** 10 §6.4's login scope denies when the limiter cannot
answer (P1-16), so a missing Redis fails every test here at the first call
rather than letting them pass quietly against a control that is not running.

**Every test uses a fresh identifier, and none makes more than nine attempts
against one.** ``test.py`` allows no way to disable the limiter — deliberately,
for the reasons recorded there — and counters live in Redis, outliving a test
run. ``UserFactory``'s sequence is not unique across runs, so the identifiers
here are generated from a UUID instead. The nine-attempt ceiling is one below
§6.4's ten, which leaves room to observe a refusal without causing one.

Settings are patched through ``monkeypatch`` rather than pytest-django's
``settings`` fixture. The two do the same thing; ``monkeypatch`` is used because
it is fully annotated, and a ``settings`` parameter would need a ``type: ignore``
on every test in this file — the tests are checked by ``mypy`` too (11 §6).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from django.conf import settings as django_settings
from django.utils import timezone

from apps.accounts.constants import AccountStatus
from apps.accounts.models import User
from apps.accounts.services import credentials
from apps.accounts.services.errors import (
    AccountClosed,
    AccountError,
    AccountSuspended,
    InvalidCredentials,
    RateLimited,
)
from tests.factories import SuspendedUserFactory, UserFactory

pytestmark = [pytest.mark.integration, pytest.mark.django_db, pytest.mark.security]

PASSWORD = "xK7#mQp2!vLz9Rt"
WRONG = "nQ8!wR3@tY6#uI9o"
"""Two visibly-synthetic constants, never realistic-looking ones, so a reviewer
scanning a diff cannot mistake either for a leaked credential (SEC-SEC-001)."""

FAILURES_TO_LOCK = 3
"""A three-failure threshold, so a lockout is reachable inside a test. The
shipped schedule is five (SEC-LOCK-001's "default 5") and would leave no room to
assert anything about the window without also asserting about the count."""

WINDOW_SECONDS = 300
"""The first lockout window in these tests. Long enough that no window closes
mid-test except the one that is closed on purpose."""


def _local_phone() -> str:
    """A valid Nigerian number in local spelling, unique per call."""
    return f"0803{uuid4().int % 10**7:07d}"


def _e164(local: str) -> str:
    """The stored form, derived here rather than imported from ``phones`` so the
    assertion is against an independent expectation."""
    return "+234" + local[1:]


def _unique_email() -> str:
    """An address no other test — and no earlier run — has used."""
    return f"member-{uuid4().hex[:12]}@example.test"


def _member(*, local: str | None = None, **overrides: Any) -> User:
    """An active member whose phone is unique unless the caller says otherwise."""
    return UserFactory(
        phone=_e164(local or _local_phone()), password=PASSWORD, **overrides
    )


def _lock_after(failures: int, *, base_seconds: int = WINDOW_SECONDS) -> dict[str, int]:
    """A lockout schedule SEC-LOCK-001's shape allows but a test can reach."""
    return {"threshold": failures, "base_seconds": base_seconds, "max_seconds": 900}


def _login_limit(limit: int, window_seconds: int = 15 * 60) -> dict[str, Any]:
    """10 §6.4's table, copied, with the login scope's ceiling replaced.

    Copied rather than mutated: ``settings.RATE_LIMITS`` is the same dict object
    ``config.settings.base`` holds, so assigning into it would rewrite the base
    settings for every module in the process.
    """
    return {
        **django_settings.RATE_LIMITS,
        "login": {"limit": limit, "window_seconds": window_seconds},
    }


def _login(
    identifier: str,
    password: str = PASSWORD,
    *,
    source_ip: str | None = None,
    now: datetime | None = None,
) -> User:
    return credentials.authenticate(
        identifier=identifier, password=password, source_ip=source_ip, now=now
    )


def _shape(identifier: str, password: str = PASSWORD) -> tuple[str, str, str]:
    """How a refusal presents to a caller: class, code, and message."""
    with pytest.raises(AccountError) as refused:
        _login(identifier, password)
    failure = refused.value
    return type(failure).__name__, failure.code, failure.detail


def _records(caplog: pytest.LogCaptureFixture, message: str) -> list[logging.LogRecord]:
    """Every record with this message so far.

    Filtered by message rather than by logger, because one call emits several
    lines and a test that took "the last record" would be asserting about
    whichever happened to come last. The fields are read with ``getattr`` because
    ``LogRecord`` does not declare the keys a caller passed in ``extra``, and
    ``mypy`` strict checks this file too.
    """
    return [r for r in caplog.records if r.getMessage() == message]


class TestSigningIn:
    def test_a_member_signs_in_with_their_phone(self) -> None:
        local = _local_phone()
        user = _member(local=local)

        assert _login(local).pk == user.pk

    def test_a_member_signs_in_with_their_phone_in_any_spelling(self) -> None:
        """FR-AUTH-001's normalisation, seen from the login side.

        A member who registered a number has no reason to type it the way the
        database stores it, and "the number you gave us does not work" is not a
        support ticket anyone can act on.
        """
        local = _local_phone()
        user = _member(local=local)

        assert _login(_e164(local)).pk == user.pk
        assert _login(f"+234 {local[1:4]} {local[4:7]} {local[7:]}").pk == user.pk

    def test_a_member_signs_in_with_their_email(self) -> None:
        """FR-AUTH-002 — phone *or* email. ``USERNAME_FIELD`` remains ``phone``:
        the address is an accepted alternative, not a second identity, which is
        why it is not stored on a second column or in a second table."""
        email = _unique_email()
        user = _member(email=email)

        assert _login(email).pk == user.pk

    def test_the_email_is_matched_case_insensitively(self) -> None:
        """The stored spelling is preserved; the *lookup* is what ignores case.

        P1-2's functional index on ``Lower(email)`` is what stops two case
        variants coexisting, and a lookup that matched exactly would then refuse
        the member whose own mail client capitalises the first letter.
        """
        user = _member(email="Adaeze.Okonkwo@Example.test")

        assert _login("adaeze.okonkwo@example.test").pk == user.pk
        assert _login("ADAEZE.OKONKWO@EXAMPLE.TEST").pk == user.pk

    def test_each_member_signs_in_to_their_own_account(self) -> None:
        """The lookup returns *the* member, not merely *a* member."""
        first_local, second_local = _local_phone(), _local_phone()
        first = _member(local=first_local)
        second = _member(local=second_local)

        assert _login(first_local).pk == first.pk
        assert _login(second_local).pk == second.pk

    def test_a_correct_password_clears_the_failure_streak(self) -> None:
        """SEC-LOCK-001 counts *consecutive* failures, so a success resets it.

        Without the reset, a member who mistypes their password once a month is
        locked out by the fifth month, for a reason nothing in the interface
        explains.
        """
        local = _local_phone()
        user = _member(local=local)
        with pytest.raises(InvalidCredentials):
            _login(local, WRONG)
        with pytest.raises(InvalidCredentials):
            _login(local, WRONG)

        _login(local)

        assert User.objects.get(pk=user.pk).failed_login_attempts == 0

    def test_an_account_with_no_usable_password_cannot_sign_in(self) -> None:
        """``set_unusable_password()`` is a real state — Django's admin creates
        accounts in it — and it must not be a password that happens to match.

        ``!``-prefixed values never verify against any input, including the empty
        one, which is the property that makes the prefix worth using.
        """
        local = _local_phone()
        user = _member(local=local)
        user.set_unusable_password()
        user.save(update_fields=["password"])

        with pytest.raises(InvalidCredentials):
            _login(local, "")
        with pytest.raises(InvalidCredentials):
            _login(local)


class TestEveryFailureIsOneFailure:
    """SEC-AUTH-005 — "identical responses whether the account exists or not"."""

    def test_six_different_failures_present_identically(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole requirement in one assertion.

        Six, and the count is worth stating because it is the point: an unknown
        phone, an unknown email, an unparseable value, a blank value, a wrong
        password and a locked account are six distinguishable situations in the
        domain and must be one indistinguishable answer here.

        Built as a comparison rather than as several assertions that each expect
        ``InvalidCredentials``, because the property is that the answers are
        *equal*. Separate assertions would all still pass if one case also
        carried a different code or message — and the code is what a client
        branches on.
        """
        monkeypatch.setattr(django_settings, "LOCKOUT", _lock_after(1))

        known = _local_phone()
        _member(local=known)
        locked = _local_phone()
        _member(local=locked)
        with pytest.raises(InvalidCredentials):
            _login(locked, WRONG)  # threshold is 1, so this locks the account

        shapes = {
            "unknown phone": _shape(_local_phone()),
            "unknown email": _shape(_unique_email()),
            "unparseable identifier": _shape("not a number at all"),
            "blank identifier": _shape(""),
            "wrong password": _shape(known, WRONG),
            # The password here is *correct*, and the answer is still this one: a
            # locked account must not become a way to learn that a guess was
            # right.
            "locked account": _shape(locked),
        }

        assert len(set(shapes.values())) == 1, shapes
        assert next(iter(shapes.values()))[1] == "invalid_credentials"

    def test_an_unparseable_number_is_not_told_apart_from_a_wrong_password(self) -> None:
        """``InvalidPhoneNumber`` must not escape this path.

        It carries the code ``invalid_phone_number``, and a client able to branch
        on that code has been told the input was read as a phone number — one bit
        more than a wrong password discloses, and one bit is all an oracle needs.
        """
        with pytest.raises(InvalidCredentials):
            _login("definitely not a number")


class TestTheLockout:
    """SEC-LOCK-001 and P1-21, from the caller's side."""

    def test_the_account_locks_on_the_configured_failure_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(django_settings, "LOCKOUT", _lock_after(FAILURES_TO_LOCK))
        local = _local_phone()
        user = _member(local=local)

        for _ in range(FAILURES_TO_LOCK - 1):
            with pytest.raises(InvalidCredentials):
                _login(local, WRONG)
        assert User.objects.get(pk=user.pk).is_locked is False

        with pytest.raises(InvalidCredentials):
            _login(local, WRONG)

        assert User.objects.get(pk=user.pk).is_locked is True

    def test_a_correct_password_does_not_open_a_locked_account(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The difference between a lockout and a delay.

        If the correct password worked, SEC-LOCK-001's window would only slow an
        attacker down — and an attacker who already had the password would not be
        slowed at all. A lockout the right credential walks through is not a
        lockout.
        """
        monkeypatch.setattr(django_settings, "LOCKOUT", _lock_after(FAILURES_TO_LOCK))
        local = _local_phone()
        _member(local=local)
        for _ in range(FAILURES_TO_LOCK):
            with pytest.raises(InvalidCredentials):
                _login(local, WRONG)

        with pytest.raises(InvalidCredentials):
            _login(local)  # the correct password

    def test_an_attempt_inside_the_window_neither_counts_nor_extends_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P1-21.

        Counting attempts made while locked lets anyone who knows a phone number
        hold that account closed indefinitely, one request per window — and a
        phone number is not a secret here, it is how a collector finds a member.
        Asserted on the deadline as well as on the counter, because a rewrite that
        kept the count but rewrote ``locked_until`` would hold the account closed
        for longer while every assertion about the number still passed.
        """
        monkeypatch.setattr(django_settings, "LOCKOUT", _lock_after(FAILURES_TO_LOCK))
        local = _local_phone()
        user = _member(local=local)
        for _ in range(FAILURES_TO_LOCK):
            with pytest.raises(InvalidCredentials):
                _login(local, WRONG)
        deadline = User.objects.get(pk=user.pk).locked_until

        with pytest.raises(InvalidCredentials):
            _login(local, WRONG)

        after = User.objects.get(pk=user.pk)
        assert after.locked_until == deadline
        assert after.failed_login_attempts == FAILURES_TO_LOCK

    def test_the_account_opens_again_when_the_window_closes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """And the streak ends with it. The window is a function of the counter,
        so a member who returns after it closes starts again from one failure
        rather than being one failure from the next lockout."""
        monkeypatch.setattr(django_settings, "LOCKOUT", _lock_after(FAILURES_TO_LOCK))
        local = _local_phone()
        user = _member(local=local)
        for _ in range(FAILURES_TO_LOCK):
            with pytest.raises(InvalidCredentials):
                _login(local, WRONG)

        User.objects.filter(pk=user.pk).update(
            locked_until=timezone.now() - timedelta(seconds=1)
        )

        assert _login(local).pk == user.pk
        assert User.objects.get(pk=user.pk).failed_login_attempts == 0

    def test_the_lockout_is_evaluated_against_the_supplied_clock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``now`` is threaded to the same comparison ``record_failure`` writes.

        The alternative — reading the clock in two places — is how a lockout ends
        up fractionally narrower than the deadline it stored: invisible in
        production, and infuriating in a test. Moving a clock past the deadline is
        also the only way to assert the expiry without sleeping.
        """
        monkeypatch.setattr(django_settings, "LOCKOUT", _lock_after(1))
        local = _local_phone()
        user = _member(local=local)
        with pytest.raises(InvalidCredentials):
            _login(local, WRONG)
        assert User.objects.get(pk=user.pk).is_locked is True

        beyond = timezone.now() + timedelta(seconds=WINDOW_SECONDS + 1)

        assert _login(local, now=beyond).pk == user.pk

    def test_a_rate_limit_refusal_is_not_a_credential_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The attempt never reached the credential, so it must not be counted.

        If it were, §6.4's limiter would be escalating SEC-LOCK-001's lockout with
        numbers the requirement does not specify — and a refusal would become a
        way to lock a member out using fewer real guesses.

        Asserted as ``== 1`` rather than ``== 0`` so that the test also proves the
        first attempt *was* counted. A test asserting only zero would pass if
        nothing were being recorded at all.
        """
        monkeypatch.setattr(django_settings, "LOCKOUT", _lock_after(FAILURES_TO_LOCK))
        monkeypatch.setattr(django_settings, "RATE_LIMITS", _login_limit(1))
        local = _local_phone()
        user = _member(local=local)

        with pytest.raises(InvalidCredentials):
            _login(local, WRONG)
        with pytest.raises(RateLimited):
            _login(local, WRONG)

        assert User.objects.get(pk=user.pk).failed_login_attempts == 1


class TestTheRateLimit:
    """10 §6.4's login scope, keyed on IP+account."""

    def test_the_attempt_over_the_ceiling_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(django_settings, "RATE_LIMITS", _login_limit(1))
        local = _local_phone()
        _member(local=local)

        with pytest.raises(InvalidCredentials):
            _login(local, WRONG)

        with pytest.raises(RateLimited) as refused:
            _login(local, WRONG)

        assert refused.value.scope == "login"
        assert refused.value.retry_after > 0

    def test_two_spellings_of_one_number_share_one_bucket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The limit is on the account, not on the string that named it.

        A limiter keyed on what was typed is a limiter an attacker multiplies by
        respelling: ``08031234567``, ``+2348031234567`` and ``+234 803 123 4567``
        are one member, and would otherwise be three allowances.
        """
        monkeypatch.setattr(django_settings, "RATE_LIMITS", _login_limit(1))
        local = _local_phone()
        _member(local=local)

        with pytest.raises(InvalidCredentials):
            _login(local, WRONG)

        with pytest.raises(RateLimited):
            _login(_e164(local), WRONG)

    def test_two_spellings_of_one_email_share_one_bucket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same property for the other identifier, where the free variants
        are capitalisation rather than punctuation — and just as free."""
        monkeypatch.setattr(django_settings, "RATE_LIMITS", _login_limit(1))
        email = _unique_email()
        _member(email=email)

        with pytest.raises(InvalidCredentials):
            _login(email, WRONG)

        with pytest.raises(RateLimited):
            _login(email.upper(), WRONG)

    def test_the_bucket_is_per_account_as_well_as_per_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§6.4 says "per IP+account", and both halves have to be in the key.

        Per-account alone misses one address spraying many accounts; per-IP alone
        misses many addresses aimed at one account. The third attempt is what
        proves the key is a *pair* rather than an account: the first address is
        refused while the second is still being served.
        """
        monkeypatch.setattr(django_settings, "RATE_LIMITS", _login_limit(1))
        local = _local_phone()
        _member(local=local)

        with pytest.raises(InvalidCredentials):
            _login(local, WRONG, source_ip="198.51.100.7")
        with pytest.raises(InvalidCredentials):
            _login(local, WRONG, source_ip="198.51.100.8")
        with pytest.raises(RateLimited):
            _login(local, WRONG, source_ip="198.51.100.7")

    def test_a_completed_sign_in_resets_the_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``ratelimit.reset``'s stated condition: a member who gets their
        password right should not stay throttled by their earlier mistakes.

        Without the reset the third attempt below is the third in a window of two
        and would be refused, so this test fails if the reset is removed.
        """
        monkeypatch.setattr(django_settings, "RATE_LIMITS", _login_limit(2))
        local = _local_phone()
        _member(local=local)

        with pytest.raises(InvalidCredentials):
            _login(local, WRONG)
        _login(local)

        with pytest.raises(InvalidCredentials):
            _login(local, WRONG)


class TestTheAccountStatus:
    """FR-AUTH-013, ordered by SEC-AUTH-005."""

    def test_a_suspended_account_with_the_wrong_password_is_not_identifiable(self) -> None:
        """The ordering, stated as a test.

        A caller who has not proved the password learns only that the credentials
        are wrong. Answering "suspended" here would make the login form an
        account-existence oracle, which is the single thing SEC-AUTH-005 exists
        to prevent.
        """
        local = _local_phone()
        SuspendedUserFactory(phone=_e164(local), password=PASSWORD)

        assert _shape(local, WRONG)[1] == "invalid_credentials"

    def test_a_suspended_account_is_told_so_once_the_password_verifies(self) -> None:
        """And the reverse. By this point the caller holds the password, so there
        is no account existence left to leak — and a suspended member told only
        "invalid credentials" will keep trying and then report a bug."""
        local = _local_phone()
        SuspendedUserFactory(phone=_e164(local), password=PASSWORD)

        with pytest.raises(AccountSuspended):
            _login(local)

    def test_a_closed_account_is_distinguishable_from_a_suspended_one(self) -> None:
        """CLOSED is terminal and SUSPENDED is not, and the member's next step
        differs: one waits for support, the other has no next step."""
        local = _local_phone()
        _member(local=local, account_status=AccountStatus.CLOSED)

        with pytest.raises(AccountClosed):
            _login(local)

    def test_a_status_this_build_does_not_know_fails_closed(self) -> None:
        """The branch is ``!= ACTIVE``, not ``== SUSPENDED``.

        A row written by a newer deploy, or by hand during an incident, must not
        authenticate merely because this build cannot name the state it is in.
        """
        local = _local_phone()
        user = _member(local=local)
        User.objects.filter(pk=user.pk).update(account_status="QUARANTINED")

        with pytest.raises(AccountSuspended):
            _login(local)

    def test_every_status_the_build_refuses_is_refused_by_both_paths(self) -> None:
        """``is_active`` and this branch read the same column, so they must agree.

        Both sides are asserted in one loop because the drift between them is the
        thing worth catching: ``ModelBackend.user_can_authenticate`` reads
        ``is_active`` while ``credentials.authenticate`` reads ``account_status``,
        and a status added to the enum with only one of the two taught about it
        would give a member refused here and admitted through Django's own
        authentication, or the reverse. Asserting the model alone — which is what
        this test did before it was rewritten — passes while the service branch
        does something else entirely, so it named a property it did not check.

        The refusal is ``AccountClosed`` when the row says CLOSED, because that
        state is terminal and the member's next step differs, and
        ``AccountSuspended`` otherwise — which is also what an unrecognised status
        receives, since the branch fails closed (P1-25).
        """
        for status in AccountStatus:
            user = _member()
            User.objects.filter(pk=user.pk).update(account_status=status)

            assert User.objects.get(pk=user.pk).is_active is (
                status == AccountStatus.ACTIVE
            )

            if status == AccountStatus.ACTIVE:
                assert _login(user.phone).pk == user.pk
                continue

            refusal: type[AccountError] = (
                AccountClosed if status == AccountStatus.CLOSED else AccountSuspended
            )
            with pytest.raises(refusal):
                _login(user.phone)

    def test_a_suspended_accounts_correct_password_still_ends_the_streak(self) -> None:
        """The counter measures *credential* failures, and this was not one.

        Clearing it before the status branch is deliberate: a suspended member who
        types their password correctly and then contacts support should not also
        be serving a lockout while they wait."""
        local = _local_phone()
        user = SuspendedUserFactory(phone=_e164(local), password=PASSWORD)
        with pytest.raises(InvalidCredentials):
            _login(local, WRONG)

        with pytest.raises(AccountSuspended):
            _login(local)

        assert User.objects.get(pk=user.pk).failed_login_attempts == 0


class TestTheLogLines:
    """SEC-AUD-001, SEC-PII-001, S1 — what is recorded, and what never is.

    The record's fields are read with ``getattr`` because ``LogRecord`` does not
    declare the keys a caller passed in ``extra``, and ``mypy`` strict checks this
    file.
    """

    def test_a_completed_sign_in_is_recorded(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)
        local = _local_phone()
        user = _member(local=local)

        _login(local)

        records = _records(caplog, "login.succeeded")
        assert len(records) == 1
        assert getattr(records[0], "user_public_id") == str(user.public_id)
        assert getattr(records[0], "method") == "phone"

    def test_an_identifier_with_no_account_is_recorded_at_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """At warning, and unconditionally — unlike a wrong password.

        A bad guess against a real account leaves a record in
        ``failed_login_attempts`` and is announced by ``account.locked`` when it
        escalates. A bad guess against no account leaves nothing at all, so this
        line is the only evidence that identifier enumeration is happening.
        """
        caplog.set_level(logging.INFO)

        with pytest.raises(InvalidCredentials):
            _login(_local_phone())

        records = _records(caplog, "login.identifier_unknown")
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert getattr(records[0], "method") == "phone"
        assert getattr(records[0], "source_ip") is None

    def test_an_identifier_that_is_not_a_number_is_recorded_as_such(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The response cannot tell the two apart; the log can, and should.

        "Someone is guessing numbers" and "a client is sending malformed input"
        are different problems with different answers, and only one of them is a
        security incident.
        """
        caplog.set_level(logging.INFO)

        with pytest.raises(InvalidCredentials):
            _login("not a number at all")

        records = _records(caplog, "login.identifier_unknown")
        assert getattr(records[0], "parseable") is False

    def test_a_refusal_inside_a_lockout_is_recorded(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hammering signal, and the only one: an attempt made while locked
        changes no column, so nothing else records that it happened."""
        caplog.set_level(logging.INFO)
        monkeypatch.setattr(django_settings, "LOCKOUT", _lock_after(1))
        local = _local_phone()
        user = _member(local=local)
        with pytest.raises(InvalidCredentials):
            _login(local, WRONG)

        with pytest.raises(InvalidCredentials):
            _login(local, WRONG)

        records = _records(caplog, "login.refused_locked")
        assert len(records) == 1
        assert getattr(records[0], "user_public_id") == str(user.public_id)

    def test_a_rate_limit_refusal_is_recorded_with_its_window(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        caplog.set_level(logging.INFO)
        monkeypatch.setattr(django_settings, "RATE_LIMITS", _login_limit(1))
        local = _local_phone()
        _member(local=local)

        with pytest.raises(InvalidCredentials):
            _login(local, WRONG)
        with pytest.raises(RateLimited):
            _login(local, WRONG)

        records = _records(caplog, "login.rate_limited")
        assert len(records) == 1
        assert getattr(records[0], "retry_after") > 0
        assert getattr(records[0], "scope") == "login"

    def test_no_record_carries_the_identifier_or_the_password(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """SEC-PII-001, DEP-LOG-003, S1.

        The sweep is over ``extra`` as well as the message, because ``extra`` is
        where this module puts its fields and where a well-meaning addition —
        ``extra={"identifier": ...}`` — would land. An unknown number is included
        on purpose: the identifier that is *not* an account is exactly the one a
        responder is most tempted to write down.

        Scoped to this package's loggers rather than to every captured record, so
        the assertion is about the lines this module writes; the project-wide
        version of it belongs to the S1 security suite.
        """
        caplog.set_level(logging.DEBUG)
        local = _local_phone()
        _member(local=local)
        unknown = _local_phone()

        with pytest.raises(InvalidCredentials):
            _login(local, WRONG)
        with pytest.raises(InvalidCredentials):
            _login(unknown)
        _login(local)

        ours = [r for r in caplog.records if r.name.startswith("apps.accounts")]
        blob = " ".join(str(record.__dict__) for record in ours)

        assert ours, "nothing was captured, so this test would prove nothing"
        assert local not in blob
        assert _e164(local) not in blob
        assert unknown not in blob
        assert WRONG not in blob
        assert PASSWORD not in blob
