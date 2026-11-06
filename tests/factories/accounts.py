"""Factories for the ``accounts`` module.

``UserFactory`` goes through ``User.objects.create_user`` rather than writing
the row directly. That matters: a factory that bypasses the manager produces
users whose password is unusable, and every authentication test written against
them then tests the wrong thing — the failure surfaces as a puzzling 401 in a
test that is nominally about something else.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import factory
from django.utils import timezone
from factory.django import DjangoModelFactory

from apps.accounts.constants import (
    AccountStatus,
    DevicePlatform,
    OTPPurpose,
    TokenPurpose,
    VerificationStatus,
)
from apps.accounts.models import (
    Device,
    OTPCode,
    User,
    UserProfile,
    UserSession,
    VerificationToken,
)

# A visibly fake constant, never a realistic-looking one: a reviewer scanning a
# diff must not have to check whether this is a leaked credential.
FACTORY_PASSWORD = "factory-password-not-a-secret"  # pragma: allowlist secret
FACTORY_PIN = "4821"


class UserFactory(DjangoModelFactory[User]):
    """A verified, active member with a usable password.

    The default is a *healthy* account, because most tests need one. Accounts
    in other states are built by overriding a single field
    (``account_status=AccountStatus.SUSPENDED``), which keeps the difference
    between the test and the norm visible at the call site.
    """

    class Meta:
        model = User

    # Nigerian E.164 numbering. A sequence rather than a random draw so that
    # two users in one test cannot collide on the unique phone constraint —
    # a flaky failure that appears once in a few hundred runs.
    phone = factory.Sequence(lambda n: f"+234803{n:07d}")
    full_name = factory.Faker("name")
    email = factory.Sequence(lambda n: f"member{n}@example.test")
    username = None
    account_status = AccountStatus.ACTIVE
    verification_status = VerificationStatus.PHONE_VERIFIED
    is_phone_verified = True
    is_email_verified = True

    @classmethod
    def _create(cls, model_class: type[User], *args: Any, **kwargs: Any) -> User:
        """Create through the manager, so the password is really hashed."""
        password = kwargs.pop("password", FACTORY_PASSWORD)
        return model_class.objects.create_user(*args, password=password, **kwargs)


class SuspendedUserFactory(UserFactory):
    """A member under an administrative hold.

    Exists so that ``is_active`` being derived from ``account_status`` can be
    tested against a named state rather than a magic string at each call site.
    """

    account_status = AccountStatus.SUSPENDED


class UnverifiedUserFactory(UserFactory):
    """A member part-way through onboarding: account fine, phone not proven."""

    verification_status = VerificationStatus.UNVERIFIED
    is_phone_verified = False


class UserProfileFactory(DjangoModelFactory[UserProfile]):
    """One profile per user — 03 §4.2, FK UNIQUE.

    A test needing two profiles for one user is testing a state the schema
    forbids, so there is no factory that produces one.
    """

    class Meta:
        model = UserProfile

    user = factory.SubFactory(UserFactory)
    occupation = factory.Faker("job")


class DeviceFactory(DjangoModelFactory[Device]):
    class Meta:
        model = Device

    user = factory.SubFactory(UserFactory)
    device_fingerprint = factory.Sequence(lambda n: f"fingerprint-{n:08d}")
    platform = DevicePlatform.ANDROID
    model = "Tecno Spark"
    os_version = "13"
    app_version = "1.0.0"


class UserSessionFactory(DjangoModelFactory[UserSession]):
    """An active session. ``expires_at`` is derived, never set independently.

    A factory that let a caller set ``expires_at`` without ``issued_at`` would
    eventually produce a session that fails the
    ``expires_at > issued_at`` CHECK constraint, and the resulting error points
    at the constraint rather than at the factory.
    """

    class Meta:
        model = UserSession

    user = factory.SubFactory(UserFactory)
    device = factory.SubFactory(DeviceFactory, user=factory.SelfAttribute("..user"))
    refresh_token_hash = factory.Sequence(lambda n: f"hashed-refresh-token-{n:08d}")

    @factory.lazy_attribute
    def issued_at(self) -> Any:
        return timezone.now()

    @factory.lazy_attribute
    def expires_at(self) -> Any:
        return self.issued_at + timedelta(days=30)


class OTPCodeFactory(DjangoModelFactory[OTPCode]):
    class Meta:
        model = OTPCode

    user = factory.SubFactory(UserFactory)
    purpose = OTPPurpose.LOGIN
    code_hash = factory.Sequence(lambda n: f"hashed-otp-{n:08d}")
    attempts = 0
    # SEC-OTP-003's cap, not the model default restated — the model default is
    # already 3, and the factory sets it explicitly so that a future change to
    # the default shows up as a failing test rather than as silently weaker
    # tests.
    max_attempts = 3

    @factory.lazy_attribute
    def destination(self) -> str:
        """The owning user's phone unless a caller overrides it."""
        return str(self.user.phone)

    @factory.lazy_attribute
    def expires_at(self) -> Any:
        return timezone.now() + timedelta(minutes=10)


class VerificationTokenFactory(DjangoModelFactory[VerificationToken]):
    class Meta:
        model = VerificationToken

    user = factory.SubFactory(UserFactory)
    purpose = TokenPurpose.EMAIL_VERIFY
    token_hash = factory.Sequence(lambda n: f"hashed-verification-token-{n:08d}")

    @factory.lazy_attribute
    def expires_at(self) -> Any:
        return timezone.now() + timedelta(hours=24)
