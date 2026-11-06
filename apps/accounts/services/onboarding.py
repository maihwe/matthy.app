"""Registration, verification delivery and atomic proof of phone ownership."""

from __future__ import annotations

from smtplib import SMTPException

from django.db import transaction
from django.utils import timezone

from apps.accounts.constants import OTPPurpose, VerificationStatus
from apps.accounts.models import OTPCode, User
from apps.accounts.services import otp, phones, registration
from apps.accounts.services.errors import (
    AuthenticationUnavailable,
    InvalidOTP,
    OTPExpired,
    OTPNotFound,
    RateLimited,
)
from config import ratelimit
from config.phone_verification import DeliveryUnavailable, PhoneCodeBackend, get_backend


def _limit(scope: str, source_ip: str | None) -> None:
    decision = ratelimit.consume(scope, source_ip or "unknown")
    if not decision.allowed:
        raise RateLimited(scope, decision.retry_after)


def _backend() -> PhoneCodeBackend:
    try:
        return get_backend()
    except (DeliveryUnavailable, ImportError, AttributeError, TypeError):
        raise AuthenticationUnavailable() from None


def _deliver(backend: PhoneCodeBackend, issued: otp.IssuedOTP | None) -> None:
    if issued is None:
        return
    try:
        backend.send(
            destination=issued.record.destination, code=issued.code, purpose=issued.record.purpose
        )
    except (DeliveryUnavailable, OSError, SMTPException):
        # Keep the account available for resend, but invalidate an undelivered code.
        OTPCode.objects.filter(pk=issued.record.pk, consumed_at__isnull=True).update(
            consumed_at=timezone.now()
        )
        raise AuthenticationUnavailable() from None


def register(
    *, full_name: str, phone: str, password: str, source_ip: str | None, email: str | None = None
) -> None:
    _limit("registration_ip", source_ip)
    backend = _backend()
    result = registration.register(full_name=full_name, phone=phone, password=password, email=email)
    _deliver(backend, result.otp)


def request_phone_code(*, phone: str, source_ip: str | None) -> None:
    _limit("registration_ip", source_ip)
    backend = _backend()
    destination = phones.normalise_phone(phone)
    user = User.objects.filter(phone=destination).first()
    if user is None or user.is_phone_verified:
        # Consume the same destination budget even when no code needs delivery.
        otp.reserve_issuance(destination)
        return
    _deliver(
        backend, otp.issue(destination=destination, purpose=OTPPurpose.PHONE_VERIFY, user=user)
    )


def verify_phone(*, phone: str, code: str, source_ip: str | None) -> User:
    _limit("phone_verification_ip", source_ip)
    destination = phones.normalise_phone(phone)
    failure: InvalidOTP | OTPExpired | OTPNotFound | None = None
    with transaction.atomic():
        # User first, as in session lifecycle operations; proof and flags commit together.
        user = User.objects.select_for_update().filter(phone=destination).first()
        if user is None or not user.is_active:
            raise InvalidOTP()
        try:
            record = otp.verify(destination=destination, purpose=OTPPurpose.PHONE_VERIFY, code=code)
        except (InvalidOTP, OTPExpired, OTPNotFound) as exc:
            # A failed guess increments the OTP counter. Commit it before propagating.
            failure = exc
        else:
            if record.user_id != user.pk:
                raise InvalidOTP()
            user.is_phone_verified = True
            user.verification_status = (
                VerificationStatus.FULLY_VERIFIED
                if user.is_email_verified
                else VerificationStatus.PHONE_VERIFIED
            )
            user.save(update_fields=["is_phone_verified", "verification_status"])
    if failure is not None:
        raise failure
    return user
