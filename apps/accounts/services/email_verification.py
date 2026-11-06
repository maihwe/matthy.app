"""Single-use proof of the email already associated with an authenticated account."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from datetime import timedelta
from smtplib import SMTPException

from django.db import transaction
from django.utils import timezone

from apps.accounts.constants import TokenPurpose, VerificationStatus
from apps.accounts.models import User, VerificationToken
from apps.accounts.services.errors import (
    AuthenticationUnavailable,
    InvalidCredentials,
    InvalidOTP,
    RateLimited,
)
from config import ratelimit
from config.phone_verification import DeliveryUnavailable, PhoneCodeBackend, get_backend

logger = logging.getLogger(__name__)
TOKEN_TTL = timedelta(hours=24)


def _limit(scope: str, identifier: str) -> None:
    decision = ratelimit.consume(scope, identifier)
    if not decision.allowed:
        raise RateLimited(scope, decision.retry_after)


def _backend() -> PhoneCodeBackend:
    try:
        return get_backend()
    except (DeliveryUnavailable, ImportError, AttributeError, TypeError):
        raise AuthenticationUnavailable() from None


def _digest(token: str, user: User) -> str:
    # The mailbox and current credential configuration must still match when
    # the link is used. A stale link cannot verify a newly assigned address.
    payload = json.dumps(
        [token, str(user.public_id), user.email, user.password, user.pin_hash, user.mfa_version]
    ).encode()
    return hashlib.sha256(b"ihweza.email-verification.v1:" + payload).hexdigest()


def request_verification(*, user: User, source_ip: str | None) -> None:
    _limit("email_verification_ip", source_ip or "unknown")
    _limit("email_verification", str(user.public_id))
    backend = _backend()
    with transaction.atomic():
        current = User.objects.select_for_update().filter(pk=user.pk).first()
        if current is None or not current.is_active:
            raise InvalidCredentials()
        if not current.email or current.is_email_verified:
            return
        # Destination budgets also bound delivery when several accounts share
        # an address over time. Hash the normalized address before using it as
        # a rate key, rather than persisting another plaintext PII copy.
        destination_key = hashlib.sha256(current.email.lower().encode()).hexdigest()
        _limit("email_verification_destination", destination_key)
        stamp = timezone.now()
        VerificationToken.objects.filter(
            user=current, purpose=TokenPurpose.EMAIL_VERIFY, consumed_at__isnull=True
        ).update(consumed_at=stamp)
        secret = f"{current.public_id}.{secrets.token_urlsafe(48)}"
        record = VerificationToken.objects.create(
            user=current,
            purpose=TokenPurpose.EMAIL_VERIFY,
            token_hash=_digest(secret, current),
            expires_at=stamp + TOKEN_TTL,
        )
        destination = current.email
    try:
        backend.send(destination=destination, code=secret, purpose=TokenPurpose.EMAIL_VERIFY)
    except (DeliveryUnavailable, OSError, SMTPException):
        # Do not leave an undelivered token live. Recheck under the same user
        # lock used for resends and confirmation so lifecycle writes serialize.
        with transaction.atomic():
            User.objects.select_for_update().get(pk=user.pk)
            VerificationToken.objects.filter(pk=record.pk, consumed_at__isnull=True).update(
                consumed_at=timezone.now()
            )
        raise AuthenticationUnavailable() from None
    logger.info("email.verification_requested", extra={"user_public_id": str(current.public_id)})


def verify(*, token: str, source_ip: str | None) -> User:
    _limit("email_verification_confirm_ip", source_ip or "unknown")
    try:
        public_id = uuid.UUID(token.split(".", 1)[0])
    except (ValueError, AttributeError):
        raise InvalidOTP() from None
    with transaction.atomic():
        user = User.objects.select_for_update().filter(public_id=public_id).first()
        if user is None or not user.is_active or not user.email or user.is_email_verified:
            raise InvalidOTP()
        record = VerificationToken.objects.filter(
            user=user,
            purpose=TokenPurpose.EMAIL_VERIFY,
            token_hash=_digest(token, user),
            consumed_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).first()
        if record is None:
            raise InvalidOTP()
        record.consumed_at = timezone.now()
        record.save(update_fields=["consumed_at"])
        user.is_email_verified = True
        user.verification_status = (
            VerificationStatus.FULLY_VERIFIED
            if user.is_phone_verified
            else VerificationStatus.EMAIL_VERIFIED
        )
        user.save(update_fields=["is_email_verified", "verification_status"])
    logger.info("email.verified", extra={"user_public_id": str(user.public_id)})
    return user
