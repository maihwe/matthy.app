"""The OTP service — SEC-OTP-001…005, and S1.

Marked ``integration`` because the controls being asserted are not all in Python:
``verify`` relies on ``SELECT ... FOR UPDATE`` to make the attempt counter
accurate under concurrency, and ``disable``-able database constraints
(``otp_codes_attempts_within_max``) are what stop a miscount becoming a security
outcome. Neither exists on SQLite.

Marked ``security`` as well, because most of these are S1's obligation — "OTP
values never appear in any API response, log, or error payload" — and SEC-OTP's
caps are security controls rather than features.

Each test uses its own destination. ``otp_issue``'s window is 3 per 15 minutes
keyed on the destination, so tests that shared one would throttle each other and
the failure would look like a bug in the service.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from django.conf import settings as django_settings
from django.contrib.auth.hashers import check_password
from django.utils import timezone

from apps.accounts.constants import OTPPurpose
from apps.accounts.models import OTPCode
from apps.accounts.services.errors import (
    InvalidOTP,
    OTPExpired,
    OTPNotFound,
    RateLimited,
)
from apps.accounts.services.otp import (
    CODE_LENGTH,
    DEFAULT_TTL,
    TTL_BY_PURPOSE,
    issue,
    verify,
)

pytestmark = [pytest.mark.integration, pytest.mark.django_db, pytest.mark.security]


def _destination() -> str:
    """A unique, phone-shaped destination.

    Phone-shaped so that the log masker takes its phone branch, which is the
    branch production takes.
    """
    return f"+2348{uuid4().int % 10**10:010d}"


def _limits(base: Any, **overrides: Any) -> dict[str, Any]:
    """The shipped table, copied, with specific scopes tightened.

    Copied rather than mutated in place: ``django_settings.RATE_LIMITS`` is the
    *same object* that ``config.settings.base`` holds, so an item assignment on it
    would rewrite the limits for every module in the process for the rest of the
    run.
    """
    limits = {scope: dict(config) for scope, config in base.items()}
    for scope, config in overrides.items():
        limits.setdefault(scope, {}).update(config)
    return limits


def _age(record: OTPCode, *, seconds: int = 1) -> None:
    """Push a code's expiry into the past.

    Aging the row rather than sleeping or freezing the clock: the assertion is
    about what ``verify`` does with an expired row, and a test that waited out a
    real 10-minute window would not be run.
    """
    OTPCode.objects.filter(pk=record.pk).update(
        expires_at=timezone.now() - timedelta(seconds=seconds)
    )


class TestGeneration:
    """SEC-OTP-001: 6 digits, from a CSPRNG."""

    def test_the_code_is_six_digits(self) -> None:
        issued = issue(destination=_destination(), purpose=OTPPurpose.LOGIN)

        assert len(issued.code) == CODE_LENGTH
        assert issued.code.isdigit()

    def test_it_comes_from_secrets_and_not_random(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """S1's premise, and the requirement's whole content.

        ``random`` is seeded from the clock and recoverable from a few outputs.
        For a value that authenticates a member, that is the difference between a
        credential and a guess. The stub also pins the zero-padding: 42 must
        become ``000042`` and not ``42`` or ``420000``.
        """
        calls: list[int] = []

        class _StubSecrets:
            @staticmethod
            def randbelow(modulus: int) -> int:
                calls.append(modulus)
                return 42

        monkeypatch.setattr("apps.accounts.services.otp.secrets", _StubSecrets)

        issued = issue(destination=_destination(), purpose=OTPPurpose.LOGIN)

        assert issued.code == "000042"
        assert calls == [10**CODE_LENGTH]

    def test_a_high_roll_still_produces_six_digits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _StubSecrets:
            @staticmethod
            def randbelow(modulus: int) -> int:
                return modulus - 1

        monkeypatch.setattr("apps.accounts.services.otp.secrets", _StubSecrets)

        assert issue(destination=_destination(), purpose=OTPPurpose.LOGIN).code == "999999"


class TestStorage:
    """SEC-OTP-002: stored hashed."""

    def test_only_the_hash_is_stored(self) -> None:
        issued = issue(destination=_destination(), purpose=OTPPurpose.LOGIN)

        stored = OTPCode.objects.get(pk=issued.record.pk)

        assert stored.code_hash != issued.code
        assert check_password(issued.code, stored.code_hash)

    def test_the_plaintext_is_in_no_column(self) -> None:
        """Not merely "not in code_hash" — nowhere in the row.

        A future column that copied the code for convenience ("last_sent_code"
        for support) would fail here, which is the point: the row is read by
        operators, included in dumps, and replicated.
        """
        issued = issue(destination=_destination(), purpose=OTPPurpose.LOGIN)

        stored = OTPCode.objects.get(pk=issued.record.pk)
        row = {
            field.name: getattr(stored, field.name)
            for field in stored._meta.fields
            if field.name != "id"
        }

        assert issued.code not in str(row)

    def test_the_attempt_cap_is_sec_otp_003s_three(self) -> None:
        issued = issue(destination=_destination(), purpose=OTPPurpose.LOGIN)

        assert issued.record.max_attempts == 3


class TestThePlaintextDoesNotLeak:
    """S1: no OTP value in any response, log, or error payload."""

    def test_the_repr_redacts_the_code(self) -> None:
        issued = issue(destination=_destination(), purpose=OTPPurpose.LOGIN)

        assert issued.code not in repr(issued)
        assert "<redacted>" in repr(issued)

    def test_the_repr_redacts_even_inside_another_structure(self) -> None:
        """The realistic leak: a repr nested in a traceback or an error message.

        ``repr`` on a list or dict of objects calls theirs, so the redaction has
        to live on the object and not on the call site.
        """
        issued = issue(destination=_destination(), purpose=OTPPurpose.LOGIN)

        assert issued.code not in repr([issued])
        # And the record's own repr, which is what a `Model.__str__` returns.
        assert issued.code not in str(issued.record)

    def test_the_code_is_not_in_what_was_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Every attribute of every record, not just the formatted message.

        ``logger.info(..., extra={...})`` puts values in the record's ``__dict__``
        and *not* in ``getMessage()``, so a check of the formatted text alone
        would pass while the code sat in the log's structured fields — which is
        exactly how a code ends up in a log aggregator.
        """
        caplog.set_level(logging.INFO, logger="apps.accounts.services.otp")
        destination = _destination()

        issued = issue(destination=destination, purpose=OTPPurpose.LOGIN)
        verify(destination=destination, purpose=OTPPurpose.LOGIN, code=issued.code)

        assert caplog.records, "nothing was logged, so this test proves nothing"
        for record in caplog.records:
            assert issued.code not in record.getMessage()
            assert issued.code not in str(record.__dict__)

    def test_the_destination_is_not_logged_in_full(self, caplog: pytest.LogCaptureFixture) -> None:
        """SEC-PII-002: PII in a log has no retention policy and no access control."""
        caplog.set_level(logging.INFO, logger="apps.accounts.services.otp")
        destination = _destination()

        issue(destination=destination, purpose=OTPPurpose.LOGIN)

        assert caplog.records
        for record in caplog.records:
            assert destination not in record.getMessage()
            assert destination not in str(record.__dict__)


class TestExpiry:
    """SEC-OTP-002, including its "shorter for high-risk actions" clause."""

    def test_the_default_window_is_ten_minutes(self) -> None:
        before = timezone.now()
        issued = issue(destination=_destination(), purpose=OTPPurpose.LOGIN)

        assert before + DEFAULT_TTL <= issued.expires_at
        assert issued.expires_at <= timezone.now() + DEFAULT_TTL

    def test_the_two_high_risk_purposes_get_five_minutes(self) -> None:
        """Parametrised over the mapping instead of a literal pair.

        A hardcoded list would fail the moment a third purpose legitimately gained
        a shorter window, and it would fail as "the test is out of date" — which
        trains people to update tests without reading them.
        ``test_only_the_documented_purposes_get_a_shorter_window`` is what holds
        the mapping to the document; this is what checks it is applied.
        """
        for purpose, ttl in TTL_BY_PURPOSE.items():
            assert ttl < DEFAULT_TTL, purpose
            issued = issue(destination=_destination(), purpose=purpose)
            assert issued.expires_at <= timezone.now() + ttl, purpose

    def test_only_the_documented_purposes_get_a_shorter_window(self) -> None:
        """A guard on the clause, not on the two values.

        "Shorter for high-risk actions" is a judgement, and this is where it was
        made. A third entry appearing here without that judgement being revisited
        is the change this catches.
        """
        assert set(TTL_BY_PURPOSE) == {OTPPurpose.TRANSACTION, OTPPurpose.PIN_RESET}


class TestIssuanceSupersedes:
    def test_a_previous_live_code_is_consumed(self) -> None:
        """Otherwise three requested codes are three valid codes.

        SEC-OTP-003 budgets 3 guesses *per code*. If each issuance left the last
        one live, a member who tapped "resend" twice would be defending six
        figures' worth of guess space with a three-attempt cap.
        """
        destination = _destination()
        first = issue(destination=destination, purpose=OTPPurpose.LOGIN)
        second = issue(destination=destination, purpose=OTPPurpose.LOGIN)

        first.record.refresh_from_db()

        assert first.record.consumed_at is not None
        assert second.record.consumed_at is None

    def test_the_superseded_code_cannot_be_verified(self) -> None:
        destination = _destination()
        first = issue(destination=destination, purpose=OTPPurpose.LOGIN)
        issue(destination=destination, purpose=OTPPurpose.LOGIN)

        with pytest.raises((OTPNotFound, InvalidOTP)):
            verify(destination=destination, purpose=OTPPurpose.LOGIN, code=first.code)

    def test_a_code_for_another_purpose_is_untouched(self) -> None:
        """The purge is scoped to (destination, purpose).

        A login code must not be invalidated by an unrelated phone-verification
        request, or a member part-way through two flows would have one of them
        silently destroyed.
        """
        destination = _destination()
        login = issue(destination=destination, purpose=OTPPurpose.LOGIN)
        issue(destination=destination, purpose=OTPPurpose.PHONE_VERIFY)

        login.record.refresh_from_db()

        assert login.record.consumed_at is None


class TestIssuanceRateLimit:
    """SEC-OTP-004."""

    def test_issuance_is_capped_per_destination(self, monkeypatch: pytest.MonkeyPatch) -> None:
        limits = _limits(django_settings.RATE_LIMITS, otp_issue={"limit": 2})
        monkeypatch.setattr(django_settings, "RATE_LIMITS", limits)
        destination = _destination()

        issue(destination=destination, purpose=OTPPurpose.LOGIN)
        issue(destination=destination, purpose=OTPPurpose.LOGIN)

        with pytest.raises(RateLimited) as refused:
            issue(destination=destination, purpose=OTPPurpose.LOGIN)

        assert refused.value.scope == "otp_issue"
        assert refused.value.retry_after > 0

    def test_a_refused_issuance_writes_no_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A refused request that still stored a code would be a code nobody sent.

        Worse than untidy: the table would show an issuance that did not happen,
        so an operator reading the abuse data would see activity that never
        reached the member.
        """
        limits = _limits(django_settings.RATE_LIMITS, otp_issue={"limit": 1})
        monkeypatch.setattr(django_settings, "RATE_LIMITS", limits)
        destination = _destination()
        issue(destination=destination, purpose=OTPPurpose.LOGIN)
        before = OTPCode.objects.count()

        with pytest.raises(RateLimited):
            issue(destination=destination, purpose=OTPPurpose.LOGIN)

        assert OTPCode.objects.count() == before

    def test_the_daily_window_is_a_second_independent_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SEC-OTP-004 names two windows; the looser one must still bite.

        With the 15-minute window opened up, only the daily cap is left, and it
        is the one that bounds SMS spend over a day.
        """
        limits = _limits(
            django_settings.RATE_LIMITS,
            otp_issue={"limit": 100},
            otp_issue_daily={"limit": 2},
        )
        monkeypatch.setattr(django_settings, "RATE_LIMITS", limits)
        destination = _destination()

        issue(destination=destination, purpose=OTPPurpose.LOGIN)
        issue(destination=destination, purpose=OTPPurpose.LOGIN)

        with pytest.raises(RateLimited) as refused:
            issue(destination=destination, purpose=OTPPurpose.LOGIN)

        assert refused.value.scope == "otp_issue_daily"

    def test_the_window_is_keyed_on_the_destination(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One member exhausting a number must not throttle another's."""
        limits = _limits(django_settings.RATE_LIMITS, otp_issue={"limit": 1})
        monkeypatch.setattr(django_settings, "RATE_LIMITS", limits)
        exhausted, fresh = _destination(), _destination()
        issue(destination=exhausted, purpose=OTPPurpose.LOGIN)

        with pytest.raises(RateLimited):
            issue(destination=exhausted, purpose=OTPPurpose.LOGIN)

        assert issue(destination=fresh, purpose=OTPPurpose.LOGIN).code


class TestVerification:
    def test_the_right_code_is_accepted(self) -> None:
        destination = _destination()
        issued = issue(destination=destination, purpose=OTPPurpose.LOGIN)

        verified = verify(destination=destination, purpose=OTPPurpose.LOGIN, code=issued.code)

        assert verified.pk == issued.record.pk

    def test_it_is_consumed_on_use(self) -> None:
        """SEC-OTP-002: single-use."""
        destination = _destination()
        issued = issue(destination=destination, purpose=OTPPurpose.LOGIN)

        verified = verify(destination=destination, purpose=OTPPurpose.LOGIN, code=issued.code)
        verified.refresh_from_db()

        assert verified.consumed_at is not None

    def test_it_cannot_be_used_twice(self) -> None:
        destination = _destination()
        issued = issue(destination=destination, purpose=OTPPurpose.LOGIN)
        verify(destination=destination, purpose=OTPPurpose.LOGIN, code=issued.code)

        with pytest.raises(OTPNotFound):
            verify(destination=destination, purpose=OTPPurpose.LOGIN, code=issued.code)

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        """A code pasted from an SMS arrives with a trailing newline.

        Rejecting it would present to the member as "the code you were just sent
        is wrong" — a bug report that cannot be reproduced by typing the code by
        hand.
        """
        destination = _destination()
        issued = issue(destination=destination, purpose=OTPPurpose.LOGIN)

        verified = verify(
            destination=destination, purpose=OTPPurpose.LOGIN, code=f"  {issued.code}\n"
        )

        assert verified.pk == issued.record.pk

    def test_a_wrong_code_is_refused(self) -> None:
        destination = _destination()
        issued = issue(destination=destination, purpose=OTPPurpose.LOGIN)
        wrong = "000000" if issued.code != "000000" else "111111"

        with pytest.raises(InvalidOTP):
            verify(destination=destination, purpose=OTPPurpose.LOGIN, code=wrong)

    def test_a_wrong_code_is_not_accepted_by_another_purpose(self) -> None:
        """A code minted for one flow cannot be spent on another.

        Otherwise a cheap flow — verifying a phone number — could mint a code and
        present it to authorise a transaction.
        """
        destination = _destination()
        issued = issue(destination=destination, purpose=OTPPurpose.PHONE_VERIFY)

        with pytest.raises(OTPNotFound):
            verify(destination=destination, purpose=OTPPurpose.TRANSACTION, code=issued.code)

    def test_a_code_for_another_destination_is_not_accepted(self) -> None:
        issued = issue(destination=_destination(), purpose=OTPPurpose.LOGIN)

        with pytest.raises(OTPNotFound):
            verify(destination=_destination(), purpose=OTPPurpose.LOGIN, code=issued.code)

    def test_an_expired_code_is_refused_and_says_so(self) -> None:
        """Distinct from "wrong code", because the member's next action differs:
        request a new one, rather than look harder at the one they have."""
        destination = _destination()
        issued = issue(destination=destination, purpose=OTPPurpose.LOGIN)
        _age(issued.record)

        with pytest.raises(OTPExpired):
            verify(destination=destination, purpose=OTPPurpose.LOGIN, code=issued.code)


class TestTheAttemptCap:
    """SEC-OTP-003, the requirement that makes a 6-digit code safe."""

    def test_each_wrong_attempt_is_counted(self) -> None:
        destination = _destination()
        issued = issue(destination=destination, purpose=OTPPurpose.LOGIN)

        with pytest.raises(InvalidOTP):
            verify(destination=destination, purpose=OTPPurpose.LOGIN, code="000000")

        issued.record.refresh_from_db()
        assert issued.record.attempts == 1

    def test_reaching_the_cap_consumes_the_code(self) -> None:
        """Not merely "stops accepting it" — the code is dead.

        The difference is observable: a rejected-but-live code makes the next
        attempt a comparison, while a consumed one cannot be tried at all.
        """
        destination = _destination()
        issued = issue(destination=destination, purpose=OTPPurpose.LOGIN)
        cap = issued.record.max_attempts

        for _ in range(cap):
            with pytest.raises(InvalidOTP):
                verify(destination=destination, purpose=OTPPurpose.LOGIN, code="000000")

        issued.record.refresh_from_db()
        assert issued.record.attempts == cap
        assert issued.record.consumed_at is not None

    def test_the_correct_code_is_refused_once_the_cap_is_reached(self) -> None:
        """The consequence, stated plainly: the member must request a new code.

        This is the behaviour that makes the cap a control rather than a
        formality — an attacker's guesses lock out the legitimate code, so the
        guess budget cannot be reset by the victim succeeding.
        """
        destination = _destination()
        issued = issue(destination=destination, purpose=OTPPurpose.LOGIN)
        for _ in range(issued.record.max_attempts):
            with pytest.raises(InvalidOTP):
                verify(destination=destination, purpose=OTPPurpose.LOGIN, code="000000")

        with pytest.raises(OTPNotFound):
            verify(destination=destination, purpose=OTPPurpose.LOGIN, code=issued.code)

    def test_the_counter_never_exceeds_the_cap(self) -> None:
        """A fourth increment would violate ``otp_codes_attempts_within_max``.

        Asserted because the failure mode without it is a database error on a
        security path — and because "exceeding it consumes the code" is only
        implementable within the constraint if the cap is checked as a ceiling
        rather than written past.
        """
        destination = _destination()
        issued = issue(destination=destination, purpose=OTPPurpose.LOGIN)
        for _ in range(issued.record.max_attempts + 3):
            with pytest.raises((InvalidOTP, OTPNotFound)):
                verify(destination=destination, purpose=OTPPurpose.LOGIN, code="000000")

        issued.record.refresh_from_db()
        assert issued.record.attempts == issued.record.max_attempts


class TestVerificationRateLimit:
    """§6.4's ``otp_verify`` scope, which is per code rather than per member."""

    def test_verification_is_capped_per_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SEC-OTP-003's cap fires first in production, and this is the backstop.

        With ``max_attempts`` at 3 and a 15-minute window at 5, the row's own cap
        is what a member meets. The window matters when the cap is raised, or when
        a bug stops incrementing it — which is the case this asserts.
        """
        limits = _limits(django_settings.RATE_LIMITS, otp_verify={"limit": 2})
        monkeypatch.setattr(django_settings, "RATE_LIMITS", limits)
        destination = _destination()
        issue(destination=destination, purpose=OTPPurpose.LOGIN)

        for _ in range(2):
            with pytest.raises(InvalidOTP):
                verify(destination=destination, purpose=OTPPurpose.LOGIN, code="000000")

        with pytest.raises(RateLimited) as refused:
            verify(destination=destination, purpose=OTPPurpose.LOGIN, code="000000")

        assert refused.value.scope == "otp_verify"

    def test_the_counter_is_keyed_on_the_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        limits = _limits(django_settings.RATE_LIMITS, otp_verify={"limit": 2})
        monkeypatch.setattr(django_settings, "RATE_LIMITS", limits)
        destination = _destination()
        issue(destination=destination, purpose=OTPPurpose.LOGIN)
        for _ in range(2):
            with pytest.raises(InvalidOTP):
                verify(destination=destination, purpose=OTPPurpose.LOGIN, code="000000")

        # A fresh code has its own budget: the member who mistyped twice is not
        # locked out of the flow, only out of that code.
        fresh = issue(destination=destination, purpose=OTPPurpose.LOGIN)

        assert verify(destination=destination, purpose=OTPPurpose.LOGIN, code=fresh.code)
