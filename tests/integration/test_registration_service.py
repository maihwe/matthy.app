"""Registration — FR-AUTH-001/007/014, S10, SEC-AUTH-004, P1-22.

Marked ``integration`` because the controls here are partly the database's: the
UNIQUE constraint on ``phone`` is what settles a race between two registrations,
and ``_create`` writes the member and their profile in one transaction. On SQLite
the race test would pass while the constraint it describes did nothing.

Marked ``security`` because the S10 assertions are security obligations, and
because the ordering asserted in ``TestRefusalsWriteNothing`` is what keeps the
validation path from becoming an enumeration oracle.

Each test uses its own number. ``otp_issue``'s window is keyed on the
destination, so tests sharing one would throttle each other — and the resulting
failure would look like a bug in the service rather than in the fixtures.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any
from uuid import uuid4

import pytest
from django.conf import settings as django_settings
from django.contrib.auth.hashers import check_password

from apps.accounts.constants import AccountStatus, OTPPurpose, VerificationStatus
from apps.accounts.models import OTPCode, User, UserProfile
from apps.accounts.services import otp as otp_service
from apps.accounts.services import registration
from apps.accounts.services.errors import InvalidPhoneNumber, RateLimited, WeakPassword
from apps.accounts.services.registration import Registration
from tests.factories import SuspendedUserFactory, UnverifiedUserFactory, UserFactory

pytestmark = [pytest.mark.integration, pytest.mark.django_db, pytest.mark.security]

STRONG_PASSWORD = "xK7#mQp2!vLz9Rt"
SECOND_PASSWORD = "bQ4$nW8*eR2!yT6u"
"""Both are visibly-synthetic constants, not realistic-looking ones, so a
reviewer scanning a diff cannot mistake either for a leaked credential
(SEC-SEC-001). Neither is similar to the fixture's name, so the similarity
validator does not enter every test through the fixture."""


def _local_phone() -> str:
    """A valid Nigerian number in local spelling: 0 + 803 + seven digits.

    Unique per call, for the reason in the module docstring.
    """
    return f"0803{uuid4().int % 10**7:07d}"


def _e164(local: str) -> str:
    """The stored form: +234, then the national number without its trunk zero.

    Computed here rather than imported from ``phones`` so that the assertion is
    against an independently-derived expectation rather than against the same
    function the service called.
    """
    return "+234" + local[1:]


def _register(phone: str, **overrides: Any) -> Registration:
    """Register with valid values, overriding only what a test is about."""
    kwargs: dict[str, Any] = {
        "full_name": "Adaeze Okonkwo",
        "phone": phone,
        "password": STRONG_PASSWORD,
    }
    kwargs.update(overrides)
    return registration.register(**kwargs)


def _limits(base: Any, **overrides: Any) -> dict[str, Any]:
    """The shipped table, copied, with specific scopes tightened.

    Copied rather than mutated: ``settings.RATE_LIMITS`` is the same dict object
    ``config.settings.base`` holds, and assigning into it would rewrite the base
    settings for every module in the process.
    """
    return {**base, **overrides}


class TestCreatingAMember:
    def test_the_number_is_stored_in_e164(self) -> None:
        """FR-AUTH-001, and the reason ``phone`` is the identity: a member may
        write their number five different ways, and only one of them can be the
        value the unique constraint sees."""
        local = _local_phone()

        result = _register(local)

        assert result.user.phone == _e164(local)

    def test_the_password_is_hashed_and_verifies(self) -> None:
        result = _register(_local_phone())

        stored = User.objects.get(pk=result.user.pk)
        assert stored.password != STRONG_PASSWORD
        assert check_password(STRONG_PASSWORD, stored.password)

    def test_the_password_appears_in_no_column(self) -> None:
        """SEC-AUTH-003 and S1.

        Swept across both rows rather than only ``users.password``: the failure
        this guards against is a helpful-looking extra column somewhere else —
        ``last_password``, a "registration payload" field — which a narrower
        assertion would not see.
        """
        result = _register(_local_phone())

        stored = User.objects.get(pk=result.user.pk)
        user_row = {f.name: getattr(stored, f.name) for f in stored._meta.fields}
        profile = UserProfile.objects.get(user=stored)
        profile_row = {f.name: getattr(profile, f.name) for f in profile._meta.fields}

        assert STRONG_PASSWORD not in str(user_row)
        assert STRONG_PASSWORD not in str(profile_row)

    def test_the_member_starts_active_and_unverified(self) -> None:
        """FR-AUTH-014: verification is tracked separately from account status.

        A member part-way through onboarding is a normal member — not suspended,
        and not verified either. Conflating either pair would make every
        unfinished signup look like either a security event or a completed one.
        """
        result = _register(_local_phone())

        assert result.user.account_status == AccountStatus.ACTIVE
        assert result.user.is_active is True
        assert result.user.is_phone_verified is False
        assert result.user.verification_status == VerificationStatus.UNVERIFIED

    def test_the_name_is_stripped(self) -> None:
        """Normalisation, which belongs here. Whether it is *present* is the
        request serializer's business (S4) — see the module notes."""
        result = _register(_local_phone(), full_name="  Adaeze Okonkwo  ")

        assert result.user.full_name == "Adaeze Okonkwo"

    def test_a_profile_is_created_with_the_member(self) -> None:
        """FR-AUTH-016. Both rows or neither: a member without a profile is an
        account whose API representation and admin page are broken in a way no
        later code path repairs."""
        result = _register(_local_phone())

        assert UserProfile.objects.filter(user=result.user).exists()

    def test_a_verification_code_is_issued_for_the_number(self) -> None:
        """FR-AUTH-007. The code's *six digits* are asserted, not its value —
        and the value is available to the caller because delivery is the
        notification layer's job (09) and Phase 1 has no such layer yet."""
        local = _local_phone()

        result = _register(local)

        assert result.otp is not None
        assert result.otp.record.purpose == OTPPurpose.PHONE_VERIFY
        assert result.otp.record.destination == _e164(local)
        assert len(result.otp.code) == 6

    def test_an_absent_email_is_stored_as_absent(self) -> None:
        result = _register(_local_phone(), email=None)

        assert result.user.email is None

    def test_a_blank_email_is_stored_as_absent(self) -> None:
        """One value for "no address", not two — otherwise ``email=""`` and
        ``email IS NULL`` both mean the same thing and every query has to check
        both."""
        result = _register(_local_phone(), email="   ")

        assert result.user.email is None

    def test_the_email_keeps_the_members_spelling(self) -> None:
        """Case-insensitive uniqueness is the functional index's job (P1-2), so
        nothing here rewrites what the member typed — and an address echoed back
        in a different case than it was given reads as a bug."""
        result = _register(_local_phone(), email="Ada@Example.test")

        assert result.user.email == "Ada@Example.test"


class TestRefusalsWriteNothing:
    def test_a_weak_password_is_refused_before_anything_is_written(self) -> None:
        """SEC-AUTH-004, and a rollback that must not be relied on: the member,
        their profile and a verification code are all absent afterwards, because
        none of them was ever written."""
        with pytest.raises(WeakPassword):
            _register(_local_phone(), password="password")

        assert User.objects.count() == 0
        assert UserProfile.objects.count() == 0
        assert OTPCode.objects.count() == 0

    def test_an_unusable_number_is_refused(self) -> None:
        with pytest.raises(InvalidPhoneNumber):
            _register("not a number")

        assert User.objects.count() == 0

    def test_the_password_is_judged_before_the_number_is_looked_up(self) -> None:
        """The order is the control, not an optimisation.

        If the lookup ran first, a taken number would take the existing-account
        path and the weak password would be *accepted* — and whether a
        submission with a weak password was refused or accepted would then be a
        reliable signal that the number is registered.
        """
        local = _local_phone()
        _register(local)

        with pytest.raises(WeakPassword):
            _register(local, password="password")


class TestANumberThatIsAlreadyRegistered:
    """S10 and P1-22."""

    def test_an_unverified_member_is_sent_another_code(self) -> None:
        """The member who began registering and did not finish.

        Refusing them would leave a half-registered account that password reset
        cannot help — there is no working account to reset into — and no route
        forward at all.
        """
        local = _local_phone()
        first = _register(local)

        second = _register(local, full_name="Someone Else", password=SECOND_PASSWORD)

        assert second.already_registered is True
        assert second.user.pk == first.user.pk
        assert second.otp is not None
        # Two rows, not one: issuance *consumes* the previous live code rather
        # than deleting it, so the earlier attempt stays visible.
        assert OTPCode.objects.filter(destination=_e164(local)).count() == 2

    def test_the_existing_members_credentials_are_untouched(self) -> None:
        """A registration form is not a way to reset somebody else's password.

        This is the assertion that matters most in this class: if the
        existing-account path wrote the submitted password, knowing a phone
        number would be enough to take the account over.
        """
        local = _local_phone()
        first = _register(local)

        _register(local, full_name="Attacker", password=SECOND_PASSWORD)

        stored = User.objects.get(pk=first.user.pk)
        assert check_password(STRONG_PASSWORD, stored.password)
        assert not check_password(SECOND_PASSWORD, stored.password)
        assert stored.full_name == "Adaeze Okonkwo"

    def test_a_verified_member_is_not_sent_a_code(self) -> None:
        """P1-22's second branch.

        A code for an account that is already verified would change nothing, and
        sending one would make this endpoint a way to make a code arrive on any
        member's phone. A member who has forgotten they registered has password
        reset (FR-AUTH-009), which is the designed path and answers identically.
        """
        local = _local_phone()
        user = UserFactory(phone=_e164(local))

        result = _register(local, password=SECOND_PASSWORD)

        assert result.already_registered is True
        assert result.user.pk == user.pk
        assert result.otp is None
        assert OTPCode.objects.count() == 0

    def test_a_suspended_number_follows_the_same_path_as_a_verified_one(self) -> None:
        """The branch is on the number, never on the account's status.

        A status-dependent answer is a second oracle: "registered, but suspended"
        identifies an account exactly as plainly as "registered" does.
        """
        local = _local_phone()
        SuspendedUserFactory(phone=_e164(local))

        result = _register(local)

        assert result.already_registered is True
        assert result.otp is None

    def test_an_unverified_member_is_reached_by_the_phone_alone(self) -> None:
        """The unverified branch, on a row that was not created by registering.

        ``_existing`` reads ``is_phone_verified`` rather than
        ``verification_status`` because the question is specifically about the
        phone (see C10); this pins that choice against a fixture that sets both
        columns.
        """
        local = _local_phone()
        user = UnverifiedUserFactory(phone=_e164(local))

        result = _register(local)

        assert result.already_registered is True
        assert result.user.pk == user.pk
        assert result.otp is not None

    def test_the_result_carries_nothing_else_a_caller_might_render(self) -> None:
        """The *shape* of the answer is part of the S10 control.

        ``already_registered`` is the only field that differs between a new
        number and a taken one, and it exists so the caller can decide what to
        write — never so it can be serialised. A future field describing *why*
        no account was created would be just as tempting to put in a response
        body, so adding one fails here and puts the decision in review.
        """
        assert {field.name for field in fields(Registration)} == {
            "user",
            "already_registered",
            "otp",
        }


class TestTheRaceBetweenTheLookupAndTheInsert:
    def test_a_number_taken_mid_request_follows_the_existing_path(self, monkeypatch) -> None:
        """The UNIQUE constraint decides who owns a number, not the earlier read.

        The race is simulated by making the *lookup* miss a row that is there —
        which is exactly what a concurrent registration sees — so the real
        ``_create`` runs and the real constraint raises inside its own atomic
        block. Patching the insert instead would test the patch.

        Without the recovery branch, the member gets a 500 for a race they did
        not cause and cannot understand, and a second member row for one number
        would be the alternative.
        """
        local = _local_phone()
        existing = UnverifiedUserFactory(phone=_e164(local))
        monkeypatch.setattr(
            registration.User.objects, "filter", lambda **kwargs: User.objects.none()
        )

        result = _register(local)

        assert result.already_registered is True
        assert result.user.pk == existing.pk
        assert User.objects.filter(phone=_e164(local)).count() == 1


class TestWhenNoCodeCanBeSent:
    def test_the_member_is_still_created(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Creation and delivery are separate steps, and the order is deliberate.

        An account that exists with no verification code is recoverable: the
        member taps resend. An account rolled back because the SMS gateway (or an
        exhausted issuance window) refused is a registration that vanished with
        an error — and there is nothing for the member to retry, because the
        form they filled in created nothing.
        """
        limits = _limits(
            django_settings.RATE_LIMITS,
            otp_issue={"limit": 1, "window_seconds": 15 * 60},
        )
        monkeypatch.setattr(django_settings, "RATE_LIMITS", limits)
        local = _local_phone()
        otp_service.issue(destination=_e164(local), purpose=OTPPurpose.PHONE_VERIFY)

        with pytest.raises(RateLimited):
            _register(local)

        assert User.objects.filter(phone=_e164(local)).exists()

    def test_a_taken_number_is_refused_the_same_way(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rate-limited answer cannot be used to probe for accounts.

        SEC-OTP-004's windows are keyed on the *destination*, not on whether an
        account exists behind it, so a new number and a taken one exhaust them
        and are refused identically. If the limiter were keyed on the account,
        this response would be the enumeration oracle S10 forbids.
        """
        limits = _limits(
            django_settings.RATE_LIMITS,
            otp_issue={"limit": 1, "window_seconds": 15 * 60},
        )
        monkeypatch.setattr(django_settings, "RATE_LIMITS", limits)
        local = _local_phone()
        _register(local)  # consumes the single allowance

        with pytest.raises(RateLimited):
            _register(local)
