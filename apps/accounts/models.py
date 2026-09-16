"""The ``accounts`` data model (03 §4.1–4.4).

Three decisions here are load-bearing, and each is a place where the obvious
implementation is wrong in a way that only shows up in production:

1. **``phone`` is the identity, not ``email``.** It is ``UNIQUE`` and
   non-nullable; ``email`` is nullable. In the target market the phone is what
   a member has, what OTPs reach, and what a collector can verify in person. An
   email-first model would make the common case — a member without email —
   the exceptional one.
2. **``is_active`` is derived, not stored.** It is a property over
   ``account_status``. A stored ``is_active`` beside ``account_status`` is two
   columns that can contradict each other, and the contradiction is a
   suspended member who can still authenticate. It also matters for a second
   reason: ``AbstractBaseUser`` declares ``is_active = True`` as a class
   attribute, so a model that does *not* override it reports every suspended
   account as active to ``ModelBackend.user_can_authenticate``.
3. **Refresh tokens and OTP codes are stored hashed.** A leaked session table
   must not be a set of usable credentials (03 §4.3).

Everything money-adjacent is absent by design: no balances, no ledger
references. Phase 1 moves no money.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, ClassVar

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.constants import (
    AccountStatus,
    DevicePlatform,
    Gender,
    OTPPurpose,
    TokenPurpose,
    TwoFactorMethod,
    VerificationStatus,
)

if TYPE_CHECKING:
    from datetime import datetime

__all__ = [
    "Device",
    "OTPCode",
    "User",
    "UserProfile",
    "UserSession",
    "VerificationToken",
]

# RFC 5321 caps an address at 254 characters. Django's `EmailField` uses 254
# too, and storing a longer value means it can never be validated.
EMAIL_MAX_LENGTH = 254

# E.164: `+` and up to 15 digits. 03 §4.1 specifies VARCHAR(20) for headroom,
# but the value stored is always normalised to E.164 — the extra width is for
# the country code and the plus, not for parentheses and spaces.
PHONE_MAX_LENGTH = 20

# Django's hasher output outgrows the column at 128 for every algorithm it
# ships, so 128 is the documented ceiling (03 §4.1). Argon2 output is shorter.
HASH_MAX_LENGTH = 128


class UserManager(BaseUserManager["User"]):
    """User creation, keyed on phone.

    ``phone`` is required and normalised by the caller (the registration
    service owns E.164 normalisation, because it needs a library and a country
    context that a model manager should not carry).
    """

    use_in_migrations = True

    def create_user(self, phone: str, password: str | None = None, **extra_fields: Any) -> User:
        if not phone:
            raise ValueError("A phone number is required; it is the login identifier.")
        if not password:
            # Not a security control — an unusable password is the *safer*
            # default, and setting one silently would create an account the
            # member did not choose a credential for.
            raise ValueError("A password is required; use set_unusable_password() explicitly.")

        user = self.model(phone=phone, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(
        self, phone: str, password: str | None = None, **extra_fields: Any
    ) -> User:
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("account_status", AccountStatus.ACTIVE)
        extra_fields.setdefault("is_phone_verified", True)
        extra_fields.setdefault("verification_status", VerificationStatus.PHONE_VERIFIED)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("A superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("A superuser must have is_superuser=True.")

        return self.create_user(phone, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """The custom user — 03 §4.1, ``users``.

    ``USERNAME_FIELD`` is ``phone``. Django's authentication machinery, its
    admin login and ``createsuperuser`` all follow that one declaration, so
    there is no second path that could authenticate by email.
    """

    public_id = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        # D6: the internal BIGINT `id` never leaves the process, and this is
        # what appears in URLs, API payloads and audit records. A sequential
        # integer in a URL leaks the member count and lets anyone walk the
        # table.
        help_text="External identifier. Safe to expose; the integer id is not.",
    )

    full_name = models.CharField(max_length=255)
    username = models.CharField(
        max_length=150,
        unique=True,
        null=True,
        blank=True,
        default=None,
        help_text="Optional. Not a login credential; phone is.",
    )
    email = models.CharField(
        max_length=EMAIL_MAX_LENGTH,
        null=True,
        blank=True,
        default=None,
        help_text="Optional. Case-insensitive uniqueness is enforced by index.",
    )
    phone = models.CharField(
        max_length=PHONE_MAX_LENGTH,
        unique=True,
        help_text="E.164 normalised, e.g. +2348012345678. The login identifier.",
    )

    pin_hash = models.CharField(
        max_length=HASH_MAX_LENGTH,
        null=True,
        blank=True,
        default=None,
        # FR-AUTH-005: hashed to the same standard as the password. Set through
        # `set_pin()` so the configured hasher list is used, rather than
        # whatever a caller happens to reach for.
        help_text="Argon2id hash of the transaction PIN. Never the PIN itself.",
    )

    account_status = models.CharField(
        max_length=16,
        choices=AccountStatus.choices,
        default=AccountStatus.ACTIVE,
    )
    verification_status = models.CharField(
        max_length=16,
        choices=VerificationStatus.choices,
        default=VerificationStatus.UNVERIFIED,
    )
    is_email_verified = models.BooleanField(default=False)
    is_phone_verified = models.BooleanField(default=False)

    two_factor_enabled = models.BooleanField(default=False)
    two_factor_method = models.CharField(
        max_length=16,
        choices=TwoFactorMethod.choices,
        null=True,
        blank=True,
        default=None,
    )

    # NFR-SEC-002 — lockout after repeated failures. Counter and deadline are
    # separate columns rather than one, because the deadline is what the
    # authentication path reads and a counter alone cannot express "until".
    failed_login_attempts = models.SmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True, default=None)

    is_staff = models.BooleanField(default=False)

    # 03 §4.1 lists both `date_registered` and `date_joined`. Only one column
    # exists, because two columns holding the same timestamp will eventually
    # disagree and nothing would say which to trust. `date_joined` is a
    # property for Django's benefit; `date_registered` is the stored truth.
    date_registered = models.DateTimeField(
        default=timezone.now,
        help_text="When the account was created. Stored as date_registered; "
        "exposed to Django as date_joined.",
    )

    # ClassVar rather than a bare assignment: mypy otherwise treats a manager
    # as a model field, and django-stubs' documented pattern for a custom
    # manager is this annotation. No `type: ignore` is needed for it, and one
    # that is not needed is an error in its own right under
    # `warn_unused_ignores`.
    objects: ClassVar[UserManager] = UserManager()

    USERNAME_FIELD = "phone"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        db_table = "users"
        verbose_name = _("user")
        verbose_name_plural = _("users")
        constraints = [
            # 03 §4.1 asks for CITEXT so that `A@x.com` and `a@x.com` cannot
            # both register. Django's CITextField is deprecated in 5.1 and
            # removed in 6.0, so the same guarantee is expressed as a
            # functional unique index (PHASE_1_PLAN P1-2).
            #
            # No `condition=` is needed: `Lower(NULL)` is NULL, and PostgreSQL
            # treats NULLs as distinct by default, so many members may have no
            # email while only one may hold any given one.
            models.UniqueConstraint(Lower("email"), name="users_email_lower_uniq"),
            # An empty string is not a phone number. Without this, the first
            # account created with `phone=""` occupies the unique slot and
            # every later such account fails with a duplicate-key error that
            # names the wrong problem.
            models.CheckConstraint(
                condition=~Q(phone=""),
                name="users_phone_not_empty",
            ),
            models.CheckConstraint(
                condition=Q(failed_login_attempts__gte=0),
                name="users_failed_login_attempts_non_negative",
            ),
        ]

    def __str__(self) -> str:
        """The public ID, never the phone.

        ``__str__`` reaches logs, admin dropdowns, error messages and
        tracebacks. A phone number in any of those is PII in a place with no
        retention policy and no access control (SEC-DATA / 10 §5.2).
        """
        return f"User({self.public_id})"

    @property
    def is_active(self) -> bool:  # type: ignore[override]
        """Derived from ``account_status`` — the single source of truth.

        ``ModelBackend.user_can_authenticate`` reads this property, so a
        suspended account cannot authenticate through any path that respects
        Django's authentication machinery. That is the whole point of deriving
        it rather than storing it.
        """
        return self.account_status == AccountStatus.ACTIVE

    @property
    def date_joined(self) -> datetime:  # type: ignore[override]
        """Django's name for ``date_registered``. See ``Meta`` notes above."""
        return self.date_registered

    def is_locked_at(self, now: datetime) -> bool:
        """Whether the lockout window is open at ``now``.

        The single definition of "locked". The property below passes the current
        clock; ``services.lockout`` passes the timestamp of the attempt it is
        processing, so that a failure and the window it is compared against are
        decided by one comparison rather than two that can disagree.
        """
        return self.locked_until is not None and self.locked_until > now

    @property
    def is_locked(self) -> bool:
        """Whether the lockout window is still open (NFR-SEC-002).

        A `locked_until` in the past is not a lock. Storing the deadline and
        evaluating it here means the account unlocks without a background job,
        which is the failure mode of a boolean `is_locked` flag that some
        scheduled task was supposed to clear.
        """
        return self.is_locked_at(timezone.now())

    def set_pin(self, raw_pin: str) -> None:
        """Hash and store the transaction PIN (FR-AUTH-005).

        Uses Django's configured hasher list, so the PIN gets Argon2id for
        exactly the same reason the password does, and moves with it when the
        list changes. A PIN is short and low-entropy; it is never stored or
        compared in any weaker form than the password.
        """
        self.pin_hash = make_password(raw_pin)

    def check_pin(self, raw_pin: str) -> bool:
        """Constant-time PIN verification, and False when no PIN is set.

        ``check_password`` against ``None`` would raise; returning False means
        "this account has no PIN" is not distinguishable from "wrong PIN" to
        the caller, which is the enumeration-resistant behaviour 10 §2
        requires.
        """
        if not self.pin_hash:
            return False
        return check_password(raw_pin, self.pin_hash)

    def clear_lockout(self) -> None:
        """Reset the failure counter. Called on a successful authentication."""
        self.failed_login_attempts = 0
        self.locked_until = None


class UserProfile(models.Model):
    """``user_profiles`` — 03 §4.2.

    Split from ``users`` so the authentication table stays narrow: these
    columns change more often and are read less often than the credential row,
    and a wide row that is hot on every authentication is a cache problem
    waiting to be discovered under load.

    ``location_id`` is **not** here yet. It is a foreign key to ``locations``,
    which lands in slice S3, and PHASE_1_PLAN P1-5 adds it then as an additive
    nullable migration. An address may legitimately be absent, so no backfill
    is required.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="profile",
        # 03 §4.2: FK UNIQUE. OneToOneField is that constraint expressed
        # structurally, so a second profile cannot be created by a code path
        # that forgot to check.
    )

    profile_picture = models.CharField(
        max_length=255,
        blank=True,
        default="",
        # A storage key, not an ImageField. ImageField requires Pillow in the
        # runtime image to validate uploads, and this container's job is
        # serving JSON — Pillow there is attack surface with no caller. The
        # upload endpoint (S4) validates and writes the object; the model
        # stores where it went.
        help_text="Storage key of the uploaded image, e.g. avatars/<uuid>.webp",
    )
    address_line = models.CharField(max_length=255, blank=True, default="")

    date_of_birth = models.DateField(null=True, blank=True, default=None)
    gender = models.CharField(max_length=24, choices=Gender.choices, blank=True, default="")
    occupation = models.CharField(max_length=100, blank=True, default="")

    next_of_kin_name = models.CharField(max_length=255, blank=True, default="")
    next_of_kin_phone = models.CharField(max_length=PHONE_MAX_LENGTH, blank=True, default="")

    kyc_level = models.SmallIntegerField(
        default=0,
        # Q5 is open: the tier thresholds are counsel's answer, not this
        # project's. The column exists so the schema does not need migrating
        # when the answer arrives; the tiers it will hold are not yet chosen.
        help_text="0 = unverified. Tier thresholds are open (Q5).",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(
        auto_now=True,
        # Maintained by the ORM. 03 §15 anticipates a database trigger for
        # `updated_at`; it arrives with the migration if a writer outside the
        # ORM ever updates this table, which today none does.
    )

    class Meta:
        db_table = "user_profiles"
        verbose_name = _("user profile")
        verbose_name_plural = _("user profiles")
        constraints = [
            models.CheckConstraint(
                condition=Q(kyc_level__gte=0),
                name="user_profiles_kyc_level_non_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"UserProfile({self.user.public_id})"


class Device(models.Model):
    """``devices`` — 03 §4.3. One row per member per client install."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="devices")

    device_fingerprint = models.CharField(
        max_length=255,
        help_text="Opaque client-generated identifier. Not a hardware serial — "
        "the app cannot read one on iOS, and pretending otherwise would build "
        "on a value that is sometimes absent.",
    )
    platform = models.CharField(max_length=16, choices=DevicePlatform.choices)
    model = models.CharField(max_length=100, blank=True, default="")
    os_version = models.CharField(max_length=50, blank=True, default="")
    app_version = models.CharField(max_length=50, blank=True, default="")
    push_token = models.CharField(max_length=255, blank=True, default="")

    last_seen_at = models.DateTimeField(null=True, blank=True, default=None)
    is_revoked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "devices"
        verbose_name = _("device")
        verbose_name_plural = _("devices")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "device_fingerprint"],
                name="devices_user_fingerprint_uniq",
            ),
        ]

    def __str__(self) -> str:
        return f"Device({self.platform}, {self.user.public_id})"


class UserSession(models.Model):
    """``user_sessions`` — 03 §4.3. A refresh token, stored hashed.

    SEC-JWT-004 requires rotation with reuse detection. Two fields make that
    possible without another table: ``revoked_at`` marks a token that was
    rotated away, and a reuse attempt is a presentation of a token whose row is
    already revoked — which is a theft signal, not a retry.

    The token *hash* is stored, never the token. A leaked session table must not
    be a set of usable credentials (03 §4.3).
    """

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sessions")
    device = models.ForeignKey(
        Device,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        default=None,
        related_name="sessions",
        # SET_NULL, not CASCADE: revoking a device must not delete the audit
        # record of the sessions it held. Those rows are what answer "was this
        # account accessed from somewhere unexpected last month".
    )

    refresh_token_hash = models.CharField(max_length=HASH_MAX_LENGTH)
    issued_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True, default=None)
    last_used_at = models.DateTimeField(null=True, blank=True, default=None)

    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
        default=None,
        # inet, not text: it validates on write and sorts correctly, so a
        # malformed address is rejected at the boundary rather than stored for
        # someone to discover in a report.
    )
    user_agent = models.CharField(max_length=512, blank=True, default="")

    class Meta:
        db_table = "user_sessions"
        verbose_name = _("user session")
        verbose_name_plural = _("user sessions")
        indexes = [
            # SEC-SES-006 — "show me my active sessions" is a query for the
            # unrevoked, unexpired minority of a table that accumulates
            # history forever, so it is a partial index.
            models.Index(
                fields=["user", "expires_at"],
                name="user_sessions_active_idx",
                condition=Q(revoked_at__isnull=True),
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(expires_at__gt=models.F("issued_at")),
                name="user_sessions_expires_after_issued",
            ),
        ]

    def __str__(self) -> str:
        return f"UserSession({self.public_id})"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and self.expires_at > timezone.now()


class OTPCode(models.Model):
    """``otp_codes`` — 03 §4.4. Single-use, expiring, hashed.

    FR-AUTH-006/008/009: both single-use (``consumed_at``) and time-limited
    (``expires_at``). ``max_attempts`` is a column rather than a constant so
    that a purpose needing stricter limits can set one without a deploy — and
    the per-purpose limit is what stops a 6-digit code being brute-forced
    inside its validity window.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        default=None,
        related_name="otp_codes",
        # Nullable because a password reset is requested by someone who may not
        # be authenticated and, for an unknown destination, has no user to
        # attach to. The row is still written: FR-AUTH-009 wants the attempt
        # recorded either way, so that enumeration and abuse are visible.
    )
    destination = models.CharField(
        max_length=EMAIL_MAX_LENGTH,
        help_text="The phone or email the code was sent to.",
    )
    purpose = models.CharField(max_length=24, choices=OTPPurpose.choices)

    code_hash = models.CharField(max_length=HASH_MAX_LENGTH)
    attempts = models.SmallIntegerField(default=0)
    max_attempts = models.SmallIntegerField(
        default=3,
        # SEC-OTP-003: "Attempts are capped (3) per code; exceeding it consumes
        # the code." The default is the security requirement's number, not a
        # rounder-looking guess — a 6-digit code in a 10-minute window is
        # guessable at 5 attempts and not at 3 (SEC-OTP-002).
        help_text="SEC-OTP-003 caps this at 3. Reaching it consumes the code.",
    )

    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True, default=None)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "otp_codes"
        verbose_name = _("OTP code")
        verbose_name_plural = _("OTP codes")
        indexes = [
            # 03 §4.4 names this one exactly. It serves the verification path:
            # find the live code for this destination and purpose.
            models.Index(
                fields=["destination", "purpose", "expires_at"],
                name="otp_codes_lookup_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(attempts__gte=0),
                name="otp_codes_attempts_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(max_attempts__gte=1),
                name="otp_codes_max_attempts_positive",
                # Zero would silently reject every code for the purpose, which
                # presents as "correct codes stopped working".
            ),
            models.CheckConstraint(
                condition=Q(attempts__lte=models.F("max_attempts")),
                name="otp_codes_attempts_within_max",
                # The ceiling is enforced by the database, not only by the
                # service: a code that exceeds its attempt limit must be dead
                # even if a future code path forgets to check.
            ),
        ]

    def __str__(self) -> str:
        # The destination is PII and the code_hash is a credential; neither
        # belongs in a log line or an admin dropdown.
        return f"OTPCode({self.purpose}, id={self.pk})"

    @property
    def is_usable(self) -> bool:
        """Unconsumed, unexpired, and within its attempt budget."""
        return (
            self.consumed_at is None
            and self.expires_at > timezone.now()
            and self.attempts < self.max_attempts
        )


class VerificationToken(models.Model):
    """``verification_tokens`` — 03 §4.4.

    Same single-use, expiring, hashed contract as ``OTPCode``, for links
    delivered by email rather than codes typed by hand. Kept separate rather
    than merged with a nullable "kind" column because the two have genuinely
    different lifecycles: a link lives for hours and is clicked once from a
    mail client, a code lives for minutes and may be retyped.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="verification_tokens")
    purpose = models.CharField(max_length=24, choices=TokenPurpose.choices)

    token_hash = models.CharField(max_length=HASH_MAX_LENGTH)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True, default=None)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "verification_tokens"
        verbose_name = _("verification token")
        verbose_name_plural = _("verification tokens")
        constraints = [
            models.CheckConstraint(
                condition=Q(expires_at__gt=models.F("created_at")),
                name="verification_tokens_expires_after_created",
            ),
        ]

    def __str__(self) -> str:
        return f"VerificationToken({self.purpose}, {self.user.public_id})"

    @property
    def is_usable(self) -> bool:
        return self.consumed_at is None and self.expires_at > timezone.now()
