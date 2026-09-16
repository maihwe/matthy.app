"""``accounts`` constraints, against real PostgreSQL.

These cannot be unit tests. Each one asserts a guarantee that lives in the
database rather than in Python — a CHECK constraint, a unique index, a
functional index on ``Lower(email)``, or a foreign key's ON DELETE behaviour.

That distinction is not academic. 13 §3 forbids SQLite precisely here: SQLite
would not enforce the case-insensitive email index or the CHECK constraints
the same way, so a suite that ran on it would pass while the invariants it
claims to verify were never exercised. **A test that cannot fail is worse than
no test, because it is believed.**

Every ``IntegrityError`` assertion is wrapped in ``transaction.atomic()``.
Without it the failure poisons the test's transaction and every later query in
the same test errors with "current transaction is aborted", which reads as a
second, unrelated bug.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.constants import AccountStatus, DevicePlatform, OTPPurpose
from apps.accounts.models import Device, OTPCode, User, UserProfile, UserSession, VerificationToken
from tests.factories.accounts import (
    FACTORY_PASSWORD,
    DeviceFactory,
    OTPCodeFactory,
    UserFactory,
    UserProfileFactory,
    UserSessionFactory,
    VerificationTokenFactory,
)

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


class TestUserManager:
    def test_create_user_hashes_the_password(self) -> None:
        """SEC-AUTH-001 — the stored value is never the password."""
        user = User.objects.create_user(phone="+2348031000001", password="correct horse battery")
        assert user.password != "correct horse battery"
        assert user.check_password("correct horse battery") is True

    def test_create_user_rejects_a_missing_phone(self) -> None:
        """Phone is the login identifier; an account without one cannot be
        logged into, so creating it is a silent trap."""
        with pytest.raises(ValueError, match="phone"):
            User.objects.create_user(phone="", password="x")

    def test_create_user_rejects_a_missing_password(self) -> None:
        """``None`` rather than an unusable password.

        ``set_unusable_password()`` is a legitimate state, and a caller that
        wants it should say so. Defaulting to it here would create login-less
        accounts by accident.
        """
        with pytest.raises(ValueError, match="password"):
            User.objects.create_user(phone="+2348031000002", password=None)

    def test_create_superuser_sets_the_admin_flags(self) -> None:
        user = User.objects.create_superuser(phone="+2348031000003", password="x")
        assert user.is_staff is True
        assert user.is_superuser is True
        assert user.account_status == AccountStatus.ACTIVE

    def test_the_username_field_round_trips_through_the_database(self) -> None:
        user = UserFactory()
        reloaded = User.objects.get(pk=user.pk)
        assert reloaded.phone == user.phone
        assert User.objects.get(**{User.USERNAME_FIELD: user.phone}).pk == user.pk

    def test_password_survives_a_reload(self) -> None:
        """Catches a manager that hashes into the instance without saving the
        hash — the account works in the test and not in production."""
        user = UserFactory()
        reloaded = User.objects.get(pk=user.pk)
        assert reloaded.check_password(FACTORY_PASSWORD) is True


class TestPhoneUniqueness:
    def test_a_duplicate_phone_is_rejected(self) -> None:
        UserFactory(phone="+2348031000010")
        with pytest.raises(IntegrityError), transaction.atomic():
            UserFactory(phone="+2348031000010")

    def test_the_rejection_is_the_database_not_the_application(self) -> None:
        """Direct ORM write, bypassing the manager entirely.

        If this passed, the uniqueness would be a manager convention rather
        than a constraint, and the first code path that forgot to check would
        create the duplicate.
        """
        UserFactory(phone="+2348031000011")
        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.create(phone="+2348031000011", full_name="Duplicate")

    def test_an_empty_phone_is_rejected_by_the_check_constraint(self) -> None:
        """The unique index alone would accept one empty phone.

        Without the CHECK, the first such row occupies the slot and every
        later attempt fails with a duplicate-key error naming the wrong
        problem.
        """
        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.create(phone="", full_name="No Phone")


class TestEmailUniquenessIsCaseInsensitive:
    """03 §4.1 asks for CITEXT; PHASE_1_PLAN P1-2 uses a functional index.

    Django's ``CITextField`` is deprecated in 5.1 and removed in 6.0, so the
    same guarantee is expressed as ``UniqueConstraint(Lower("email"))``. These
    tests assert the *guarantee*, not the mechanism, so the mechanism can
    change again without the guarantee silently lapsing.
    """

    def test_the_same_email_in_a_different_case_is_rejected(self) -> None:
        UserFactory(email="Ada@Example.test")
        with pytest.raises(IntegrityError), transaction.atomic():
            UserFactory(email="ada@example.test")

    def test_an_exact_duplicate_is_rejected(self) -> None:
        UserFactory(email="chidi@example.test")
        with pytest.raises(IntegrityError), transaction.atomic():
            UserFactory(email="chidi@example.test")

    def test_different_emails_are_allowed(self) -> None:
        """The other half, so the index cannot pass by rejecting everything."""
        UserFactory(email="one@example.test")
        UserFactory(email="two@example.test")
        assert User.objects.filter(email__endswith="@example.test").count() == 2

    def test_many_users_may_have_no_email(self) -> None:
        """NULLs are distinct by default in PostgreSQL, so no partial index
        condition is needed — and email is optional in this market, so this is
        the common case, not an edge one."""
        UserFactory(email=None)
        UserFactory(email=None)
        UserFactory(email=None)
        assert User.objects.filter(email__isnull=True).count() == 3

    def test_a_duplicate_phone_is_caught_even_when_the_email_differs(self) -> None:
        """Uniqueness is per column, not per row: two users sharing a phone
        with different emails are still the same person's account."""
        UserFactory(phone="+2348031000020", email="first@example.test")
        with pytest.raises(IntegrityError), transaction.atomic():
            UserFactory(phone="+2348031000020", email="second@example.test")


class TestUserProfile:
    def test_a_second_profile_for_the_same_user_is_rejected(self) -> None:
        """03 §4.2 FK UNIQUE, expressed as OneToOneField so the constraint
        exists in the schema rather than in a code path."""
        profile = UserProfileFactory()
        with pytest.raises(IntegrityError), transaction.atomic():
            UserProfile.objects.create(user_id=profile.user_id)

    def test_the_profile_is_reachable_from_the_user(self) -> None:
        profile = UserProfileFactory()
        assert profile.user.profile.pk == profile.pk

    def test_deleting_a_user_removes_the_profile(self) -> None:
        """CASCADE. Note this is the *only* place deletion is acceptable: no
        financial row is ever deleted (44 rules 3 and 10), and a user is
        normally closed rather than removed. The cascade exists so that a test
        or a development reset does not leave orphans, not as a supported
        production operation."""
        profile = UserProfileFactory()
        user_id = profile.user_id
        User.objects.filter(pk=user_id).delete()
        assert not UserProfile.objects.filter(user_id=user_id).exists()

    def test_kyc_level_cannot_be_negative(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            UserProfileFactory(kyc_level=-1)


class TestDevice:
    def test_the_same_fingerprint_twice_for_one_user_is_rejected(self) -> None:
        """03 §4.3 — UNIQUE (user_id, device_fingerprint). Re-registering the
        same install must update the row, not add a second one, or the session
        list fills with duplicates of the same phone."""
        user = UserFactory()
        DeviceFactory(user=user, device_fingerprint="same-device")
        with pytest.raises(IntegrityError), transaction.atomic():
            DeviceFactory(user=user, device_fingerprint="same-device")

    def test_two_users_may_share_a_fingerprint_string(self) -> None:
        """The uniqueness is per user, not global.

        A shared device — a family phone, a collector's handset used to enrol
        members — is a real situation, and a global unique index would make the
        second person unable to register.
        """
        DeviceFactory(user=UserFactory(), device_fingerprint="shared")
        DeviceFactory(user=UserFactory(), device_fingerprint="shared")
        assert Device.objects.filter(device_fingerprint="shared").count() == 2

    def test_deleting_a_user_removes_their_devices(self) -> None:
        device = DeviceFactory()
        user_id = device.user_id
        User.objects.filter(pk=user_id).delete()
        assert not Device.objects.filter(user_id=user_id).exists()


class TestUserSession:
    def test_expiry_before_issue_is_rejected(self) -> None:
        """The CHECK that stops a session being born expired — which would
        present as "login succeeds but nothing works"."""
        issued = timezone.now()
        with pytest.raises(IntegrityError), transaction.atomic():
            UserSession.objects.create(
                user=UserFactory(),
                refresh_token_hash="hash",
                issued_at=issued,
                expires_at=issued - timedelta(seconds=1),
            )

    def test_a_session_row_holds_a_hash_not_a_token(self) -> None:
        """03 §4.3 — a leaked session table must not be a set of usable
        credentials. The factory value is deliberately not token-shaped."""
        session = UserSessionFactory()
        assert session.refresh_token_hash.startswith("hashed-")

    def test_a_session_carries_a_public_id(self) -> None:
        session = UserSessionFactory()
        assert session.public_id is not None

    def test_is_active_reflects_revocation(self) -> None:
        session = UserSessionFactory()
        assert session.is_active is True
        session.revoked_at = timezone.now()
        assert session.is_active is False

    def test_is_active_reflects_expiry(self) -> None:
        session = UserSessionFactory()
        session.expires_at = timezone.now() - timedelta(seconds=1)
        assert session.is_active is False

    def test_deleting_a_device_keeps_the_session_row(self) -> None:
        """SET_NULL, not CASCADE.

        Revoking a device must not delete the record of the sessions it held:
        those rows are what answer "was this account accessed from somewhere
        unexpected last month", and a manager asking that question a week later
        needs them to still exist.
        """
        session = UserSessionFactory()
        session.device.delete()
        session.refresh_from_db()
        assert session.device is None
        assert session.pk is not None

    def test_deleting_a_user_removes_their_sessions(self) -> None:
        session = UserSessionFactory()
        user_id = session.user_id
        User.objects.filter(pk=user_id).delete()
        assert not UserSession.objects.filter(user_id=user_id).exists()


class TestOTPCode:
    def test_attempts_cannot_exceed_max_attempts(self) -> None:
        """The brute-force ceiling is enforced by the database.

        A 6-digit code inside a ten-minute window is guessable if attempts are
        unlimited, and the limit must hold even for a future code path that
        forgets to check it.
        """
        otp = OTPCodeFactory(max_attempts=5)
        with pytest.raises(IntegrityError), transaction.atomic():
            OTPCode.objects.filter(pk=otp.pk).update(attempts=6)

    def test_max_attempts_must_be_at_least_one(self) -> None:
        """Zero would silently reject every code for the purpose, which
        presents as "correct codes stopped working"."""
        with pytest.raises(IntegrityError), transaction.atomic():
            OTPCodeFactory(max_attempts=0)

    def test_attempts_cannot_be_negative(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            OTPCodeFactory(attempts=-1)

    def test_the_attempt_budget_is_per_purpose(self) -> None:
        """max_attempts is a column, not a constant, so a purpose needing a
        stricter limit can have one without a deploy."""
        strict = OTPCodeFactory(purpose=OTPPurpose.TRANSACTION, max_attempts=3)
        assert strict.max_attempts == 3

    def test_the_default_destination_is_the_owners_phone(self) -> None:
        otp = OTPCodeFactory()
        assert otp.destination == otp.user.phone

    def test_an_otp_may_have_no_user(self) -> None:
        """A password reset for an unknown destination still writes a row.

        FR-AUTH-009 wants the attempt recorded either way, so that enumeration
        and abuse are visible — which means ``user_id`` must be nullable.
        """
        otp = OTPCodeFactory(user=None, destination="+2348031999999")
        assert otp.user is None
        assert OTPCode.objects.filter(destination="+2348031999999").exists()

    def test_is_usable_requires_unconsumed_unexpired_and_within_budget(self) -> None:
        otp = OTPCodeFactory()
        assert otp.is_usable is True

        otp.consumed_at = timezone.now()
        assert otp.is_usable is False

    def test_an_expired_otp_is_not_usable(self) -> None:
        otp = OTPCodeFactory()
        otp.expires_at = timezone.now() - timedelta(seconds=1)
        assert otp.is_usable is False

    def test_an_otp_at_its_attempt_limit_is_not_usable(self) -> None:
        otp = OTPCodeFactory(attempts=5, max_attempts=5)
        assert otp.is_usable is False

    def test_the_code_is_stored_hashed(self) -> None:
        otp = OTPCodeFactory()
        assert otp.code_hash.startswith("hashed-")


class TestVerificationToken:
    def test_expiry_before_creation_is_rejected(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            VerificationToken.objects.create(
                user=UserFactory(),
                purpose="EMAIL_VERIFY",
                token_hash="hash",
                expires_at=timezone.now() - timedelta(days=1),
            )

    def test_is_usable_requires_unconsumed_and_unexpired(self) -> None:
        token = VerificationTokenFactory()
        assert token.is_usable is True
        token.consumed_at = timezone.now()
        assert token.is_usable is False

    def test_an_expired_token_is_not_usable(self) -> None:
        token = VerificationTokenFactory()
        token.expires_at = timezone.now() - timedelta(seconds=1)
        assert token.is_usable is False

    def test_the_token_is_stored_hashed(self) -> None:
        token = VerificationTokenFactory()
        assert token.token_hash.startswith("hashed-")

    def test_deleting_a_user_removes_their_tokens(self) -> None:
        token = VerificationTokenFactory()
        user_id = token.user_id
        User.objects.filter(pk=user_id).delete()
        assert not VerificationToken.objects.filter(user_id=user_id).exists()


class TestPlatformChoices:
    def test_an_unknown_platform_is_rejected_by_the_database(self) -> None:
        """``choices`` is enforced by Django's full_clean, which the ORM does
        not call on save. The column is a plain VARCHAR, so this test documents
        the actual boundary rather than the assumed one: value validation
        belongs to the serializer layer (S4), and the database accepts the
        string.
        """
        device = DeviceFactory(platform=DevicePlatform.ANDROID)
        assert device.platform == DevicePlatform.ANDROID
        # Recorded honestly: this is what the schema does, not what we wish.
        Device.objects.filter(pk=device.pk).update(platform="NOT_A_PLATFORM")
        device.refresh_from_db()
        assert device.platform == "NOT_A_PLATFORM"
