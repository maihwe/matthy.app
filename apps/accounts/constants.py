"""Closed vocabularies for the ``accounts`` module.

Values are stored as short uppercase strings rather than integers (03 §4.1).
The reason is operational rather than aesthetic: an operator reading a
production row, a log line, or a `psql` session sees ``SUSPENDED``, and a
database dump is legible without a lookup table that may itself have drifted.

Every vocabulary here is a ``TextChoices`` so that Django enforces it at the
form and serializer layer, and ``models.CharField(choices=...)`` keeps the
allowed set visible in the migration.
"""

from __future__ import annotations

from django.db import models

__all__ = [
    "AccountStatus",
    "DevicePlatform",
    "Gender",
    "OTPPurpose",
    "TokenPurpose",
    "TwoFactorMethod",
    "VerificationStatus",
]


class AccountStatus(models.TextChoices):
    """Whether the account may be used at all (03 §4.1).

    ``SUSPENDED`` is the administrative hold — a member under investigation or
    with a disputed balance. ``CLOSED`` is terminal and is never reached by
    deletion: 44 rule 10 requires a complete audit trail, so an account is
    closed, never removed. ``User.is_active`` is derived from this single
    column rather than stored beside it, so the two cannot disagree.
    """

    ACTIVE = "ACTIVE", "Active"
    SUSPENDED = "SUSPENDED", "Suspended"
    CLOSED = "CLOSED", "Closed"


class VerificationStatus(models.TextChoices):
    """Verification progress, tracked separately from account status.

    FR-AUTH-014 requires the two to be independent: a member who has not yet
    verified their phone is a normal member part-way through onboarding, not a
    suspended one. Conflating them would make every unverified signup look like
    a security event.
    """

    UNVERIFIED = "UNVERIFIED", "Unverified"
    PHONE_VERIFIED = "PHONE_VERIFIED", "Phone verified"
    EMAIL_VERIFIED = "EMAIL_VERIFIED", "Email verified"
    FULLY_VERIFIED = "FULLY_VERIFIED", "Fully verified"


class TwoFactorMethod(models.TextChoices):
    """SEC-MFA — how the second factor is delivered."""

    SMS = "SMS", "SMS"
    EMAIL = "EMAIL", "Email"
    TOTP = "TOTP", "Authenticator app"


class Gender(models.TextChoices):
    """Optional, and deliberately not a binary choice.

    A field that offers only two options when neither is correct is a field
    people fill in wrongly, and 03 §12.1 logs changes to it. PREFER_NOT_TO_SAY
    is a real answer, not a missing value.
    """

    MALE = "MALE", "Male"
    FEMALE = "FEMALE", "Female"
    OTHER = "OTHER", "Other"
    PREFER_NOT_TO_SAY = "PREFER_NOT_TO_SAY", "Prefer not to say"


class OTPPurpose(models.TextChoices):
    """What a one-time code authorises.

    Stored per-purpose so that an OTP issued to log in cannot be replayed to
    authorise a transfer: the consuming service checks the purpose, and a code
    minted for one cannot be presented for another.
    """

    LOGIN = "LOGIN", "Login"
    PHONE_VERIFY = "PHONE_VERIFY", "Phone verification"
    EMAIL_VERIFY = "EMAIL_VERIFY", "Email verification"
    PASSWORD_RESET = "PASSWORD_RESET", "Password reset"  # pragma: allowlist secret
    PIN_RESET = "PIN_RESET", "PIN reset"
    TRANSACTION = "TRANSACTION", "Transaction authorisation"


class TokenPurpose(models.TextChoices):
    """What a verification token authorises (03 §4.4)."""

    EMAIL_VERIFY = "EMAIL_VERIFY", "Email verification"
    PHONE_VERIFY = "PHONE_VERIFY", "Phone verification"
    PASSWORD_RESET = "PASSWORD_RESET", "Password reset"  # pragma: allowlist secret


class DevicePlatform(models.TextChoices):
    """The client platform a device registered from.

    Drives nothing today; recorded because SEC-SES-006 requires a member to be
    able to see and revoke their sessions, and "Android, Lagos, 2 days ago" is
    a decision they can make where "unknown device" is not.
    """

    ANDROID = "ANDROID", "Android"
    IOS = "IOS", "iOS"
    WEB = "WEB", "Web"
    USSD = "USSD", "USSD"
