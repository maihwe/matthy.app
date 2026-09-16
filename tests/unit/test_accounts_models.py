"""``accounts`` model logic that needs no database.

The centrepiece is ``is_active``. It is a property over ``account_status``
rather than a stored column, and the reason is not tidiness: a stored
``is_active`` beside ``account_status`` can disagree with it, and the
disagreement that matters is a suspended member who can still authenticate.
``ModelBackend.user_can_authenticate`` reads ``is_active``, so these tests are
asserting the lock on the front door.

No database is needed for any of it — a property and a hasher are pure.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth.models import ModelBackend
from django.db.models import Model
from django.utils import timezone

from apps.accounts.constants import AccountStatus, OTPPurpose, VerificationStatus
from apps.accounts.models import (
    Device,
    OTPCode,
    User,
    UserProfile,
    UserSession,
    VerificationToken,
)

pytestmark = pytest.mark.unit


class TestLoginIdentifier:
    """The phone is the identity (03 §4.1), and that is a single declaration."""

    def test_username_field_is_phone(self) -> None:
        """Django's whole authentication stack follows ``USERNAME_FIELD``.

        There is no second path that authenticates by email precisely because
        this is not a list of acceptable identifiers but one field.
        """
        assert User.USERNAME_FIELD == "phone"

    def test_full_name_is_required_to_create_a_superuser(self) -> None:
        assert User.REQUIRED_FIELDS == ["full_name"]

    def test_phone_is_unique_and_not_nullable(self) -> None:
        field = User._meta.get_field("phone")
        assert field.unique is True
        assert field.null is False

    def test_email_is_nullable(self) -> None:
        """Email is optional in §4, and a non-null email column would make the
        common case — a member without one — impossible to store."""
        field = User._meta.get_field("email")
        assert field.null is True


class TestIsActiveIsDerived:
    """The property that keeps a suspended member out."""

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (AccountStatus.ACTIVE, True),
            (AccountStatus.SUSPENDED, False),
            (AccountStatus.CLOSED, False),
        ],
    )
    def test_is_active_follows_account_status(
        self, status: AccountStatus, expected: bool
    ) -> None:
        assert User(phone="+2348030000001", account_status=status).is_active is expected

    def test_a_suspended_user_cannot_authenticate(self) -> None:
        """Stated against Django's own backend, not against the property.

        Asserting ``is_active is False`` only proves the property agrees with
        the constant. This proves Django reads it — which is the link that
        would break if a future refactor gave ``is_active`` a stored column
        defaulting to True.
        """
        backend = ModelBackend()
        suspended = User(phone="+2348030000002", account_status=AccountStatus.SUSPENDED)
        assert backend.user_can_authenticate(suspended) is False

    def test_an_active_user_can_authenticate(self) -> None:
        """The other half, so the test above cannot pass by rejecting
        everyone."""
        backend = ModelBackend()
        active = User(phone="+2348030000003", account_status=AccountStatus.ACTIVE)
        assert backend.user_can_authenticate(active) is True

    def test_the_default_status_is_active(self) -> None:
        assert User(phone="+2348030000004").account_status == AccountStatus.ACTIVE

    def test_is_active_is_not_a_database_column(self) -> None:
        """The regression guard.

        If someone adds ``is_active`` as a field, this fails and the failure
        message points at the reason rather than leaving two columns to drift
        apart.
        """
        field_names = {f.name for f in User._meta.get_fields()}
        assert "is_active" not in field_names


class TestDateJoined:
    """One column, two names."""

    def test_date_joined_aliases_date_registered(self) -> None:
        """03 §4.1 lists both. Only one is stored: two columns holding the same
        instant will drift, and nothing would say which to trust."""
        now = timezone.now()
        user = User(phone="+2348030000005", date_registered=now)
        assert user.date_joined == now

    def test_only_one_of_them_is_a_column(self) -> None:
        field_names = {f.name for f in User._meta.get_fields()}
        assert "date_registered" in field_names
        assert "date_joined" not in field_names

    def test_date_registered_defaults_to_now(self) -> None:
        user = User(phone="+2348030000006")
        assert user.date_registered is not None
        assert abs((timezone.now() - user.date_registered).total_seconds()) < 5


class TestPublicId:
    """D6 — the integer never leaves the process."""

    def test_a_public_id_is_generated_on_instantiation(self) -> None:
        user = User(phone="+2348030000007")
        assert isinstance(user.public_id, uuid.UUID)

    def test_public_ids_differ_between_users(self) -> None:
        """A sequential or shared public_id would expose the member count and
        let anyone walk the table."""
        first = User(phone="+2348030000008").public_id
        second = User(phone="+2348030000009").public_id
        assert first != second

    def test_public_id_is_not_editable(self) -> None:
        field = User._meta.get_field("public_id")
        assert field.unique is True
        assert field.editable is False


class TestStrIsNotPii:
    """``__str__`` reaches logs, admin dropdowns and tracebacks."""

    def test_str_uses_the_public_id(self) -> None:
        user = User(phone="+2348030000010", full_name="Ada Okeke")
        assert str(user) == f"User({user.public_id})"

    def test_str_does_not_contain_the_phone(self) -> None:
        """A phone number in a log line is PII in a place with no retention
        policy and no access control (SEC-DATA, 10 §5.2)."""
        phone = "+2348030000011"
        assert phone not in str(User(phone=phone))

    def test_str_does_not_contain_the_name(self) -> None:
        user = User(phone="+2348030000012", full_name="Chidi Nwosu")
        assert "Chidi" not in str(user)


class TestPin:
    """FR-AUTH-005 — the PIN is hashed to the password standard."""

    def test_check_pin_is_false_when_no_pin_is_set(self) -> None:
        """Not an exception.

        ``check_password`` against ``None`` would raise, and the resulting 500
        is both an availability bug and an oracle: it distinguishes "no PIN
        configured" from "wrong PIN" for anyone who can read status codes.
        """
        assert User(phone="+2348030000013").check_pin("0000") is False

    def test_a_set_pin_verifies(self) -> None:
        user = User(phone="+2348030000014")
        user.set_pin("4821")
        assert user.check_pin("4821") is True

    def test_a_wrong_pin_does_not_verify(self) -> None:
        user = User(phone="+2348030000015")
        user.set_pin("4821")
        assert user.check_pin("1234") is False

    def test_the_pin_is_not_stored_in_clear(self) -> None:
        """SEC-SEC-001 — a leaked database must not be a set of usable PINs."""
        user = User(phone="+2348030000016")
        user.set_pin("4821")
        assert user.pin_hash is not None
        assert "4821" not in user.pin_hash

    def test_the_pin_hash_is_verifiable_by_djangos_hasher(self) -> None:
        """Confirms the hash is in Django's format rather than some ad-hoc
        encoding, so it moves with ``PASSWORD_HASHERS`` when the list changes.

        The *ordering* of the hasher list is asserted separately in
        test_settings_contract.py; this test runs under the test settings, which
        deliberately use a fast hasher.
        """
        from django.contrib.auth.hashers import identify_hasher

        user = User(phone="+2348030000017")
        user.set_pin("4821")
        assert user.pin_hash is not None
        # Raises UnrecognizedHashError if the value is not a Django hash.
        assert identify_hasher(user.pin_hash) is not None


class TestLockout:
    """NFR-SEC-002 — a deadline, not a flag."""

    def test_a_future_deadline_is_locked(self) -> None:
        user = User(
            phone="+2348030000018",
            locked_until=timezone.now() + timedelta(minutes=15),
        )
        assert user.is_locked is True

    def test_a_past_deadline_is_not_locked(self) -> None:
        """The account unlocks as time passes, with no background job.

        A boolean ``is_locked`` flag needs something to clear it, and the
        failure mode of that design is an account locked forever because the
        clearing job died — reported, inevitably, as "I can't log in".
        """
        user = User(
            phone="+2348030000019",
            locked_until=timezone.now() - timedelta(seconds=1),
        )
        assert user.is_locked is False

    def test_no_deadline_is_not_locked(self) -> None:
        assert User(phone="+2348030000020").is_locked is False

    def test_clear_lockout_resets_both_the_counter_and_the_deadline(self) -> None:
        user = User(
            phone="+2348030000021",
            failed_login_attempts=5,
            locked_until=timezone.now() + timedelta(minutes=15),
        )
        user.clear_lockout()
        assert user.failed_login_attempts == 0
        assert user.locked_until is None
        assert user.is_locked is False

    def test_the_failure_counter_defaults_to_zero(self) -> None:
        assert User(phone="+2348030000022").failed_login_attempts == 0


class TestVerificationStatusIsIndependent:
    """FR-AUTH-014 — verification and account status are different questions."""

    def test_an_unverified_user_is_still_active(self) -> None:
        """The bug this prevents: treating every unverified signup as a
        security event, so the real ones stop standing out."""
        user = User(
            phone="+2348030000023",
            account_status=AccountStatus.ACTIVE,
            verification_status=VerificationStatus.UNVERIFIED,
        )
        assert user.is_active is True
        assert user.verification_status == VerificationStatus.UNVERIFIED

    def test_verification_defaults_to_unverified(self) -> None:
        user = User(phone="+2348030000024")
        assert user.verification_status == VerificationStatus.UNVERIFIED
        assert user.is_phone_verified is False
        assert user.is_email_verified is False

    def test_two_factor_is_off_by_default(self) -> None:
        user = User(phone="+2348030000025")
        assert user.two_factor_enabled is False
        assert user.two_factor_method is None


class TestOTPDefaults:
    """SEC-OTP-003's cap is a default in the schema, so it is asserted here.

    A silent change to this number would weaken every OTP path at once, and no
    functional test would notice: they all pass an explicit ``max_attempts``.
    """

    def test_max_attempts_defaults_to_the_security_requirement(self) -> None:
        """SEC-OTP-003: attempts capped at 3, not a rounder-looking 5.

        A 6-digit code inside a 10-minute window is guessable at 5 attempts and
        not at 3 (SEC-OTP-002).
        """
        assert OTPCode._meta.get_field("max_attempts").default == 3

    def test_attempts_start_at_zero(self) -> None:
        assert OTPCode._meta.get_field("attempts").default == 0

    def test_a_new_code_is_usable(self) -> None:
        """Recorded as a property, so a code that is born unusable is caught
        here rather than as "correct codes stopped working" in production."""
        assert OTPCode(purpose=OTPPurpose.LOGIN).attempts == 0


class TestTableNames:
    """The physical names are the contract in 03 §4."""

    @pytest.mark.parametrize(
        ("model", "table"),
        [
            (User, "users"),
            (UserProfile, "user_profiles"),
            (Device, "devices"),
            (UserSession, "user_sessions"),
            (OTPCode, "otp_codes"),
            (VerificationToken, "verification_tokens"),
        ],
    )
    def test_db_table_matches_the_design(self, model: type[Model], table: str) -> None:
        assert model._meta.db_table == table

    def test_the_app_label_is_accounts(self) -> None:
        """``AUTH_USER_MODEL = "accounts.User"`` depends on this label, and a
        silent change to it would break migrations with an error message that
        does not explain itself."""
        assert User._meta.app_label == "accounts"
