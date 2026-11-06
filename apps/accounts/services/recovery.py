"""Verified-channel password recovery and atomic password/PIN changes."""

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

from apps.accounts.constants import OTPPurpose, TokenPurpose
from apps.accounts.models import OTPCode, User, VerificationToken
from apps.accounts.services import credentials, lockout, otp, passwords, phones, sessions
from apps.accounts.services.errors import (
    AuthenticationUnavailable,
    InvalidCredentials,
    InvalidOTP,
    InvalidPin,
    OTPExpired,
    OTPNotFound,
    RateLimited,
    WeakPassword,
)
from apps.audit.constants import ActorType
from apps.audit.services import record as record_audit
from config import ratelimit
from config.phone_verification import DeliveryUnavailable, PhoneCodeBackend, get_backend

logger = logging.getLogger(__name__)
RESET_TTL = timedelta(minutes=10)


def _limit(scope: str, identifier: str) -> None:
    decision = ratelimit.consume(scope, identifier)
    if not decision.allowed:
        raise RateLimited(scope, decision.retry_after)


def _backend() -> PhoneCodeBackend:
    try:
        return get_backend()
    except (DeliveryUnavailable, ImportError, AttributeError, TypeError):
        raise AuthenticationUnavailable() from None


def _send_reset(
    backend: PhoneCodeBackend, *, destination: str, secret: str, record: OTPCode | VerificationToken
) -> None:
    try:
        backend.send(destination=destination, code=secret, purpose=TokenPurpose.PASSWORD_RESET)
    except (DeliveryUnavailable, OSError, SMTPException):
        record.consumed_at = timezone.now()
        record.save(update_fields=["consumed_at"])
        raise AuthenticationUnavailable() from None


def _email_token_digest(token: str, user: User) -> str:
    # Bind the emailed secret to its destination and credential state. An email
    # or credential change invalidates an earlier link even outside these APIs.
    payload = json.dumps([token, user.email, user.password, user.pin_hash]).encode()
    return hashlib.sha256(b"ihweza.password-reset.v1:" + payload).hexdigest()


def _invalidate_recovery(user: User) -> None:
    stamp = timezone.now()
    OTPCode.objects.filter(
        user=user,
        purpose__in=[OTPPurpose.PASSWORD_RESET, OTPPurpose.PIN_RESET],
        consumed_at__isnull=True,
    ).update(consumed_at=stamp)
    VerificationToken.objects.filter(
        user=user, purpose=TokenPurpose.PASSWORD_RESET, consumed_at__isnull=True
    ).update(consumed_at=stamp)


def request_reset(*, identifier: str, source_ip: str | None) -> None:
    _limit("password_reset_ip", source_ip or "unknown")
    backend = _backend()
    user, normalized = credentials.resolve_identifier(identifier)
    _limit("password_reset", str(user.public_id) if user else normalized)
    by_email = "@" in normalized
    if user is None:
        if not by_email:
            otp.reserve_issuance(normalized)
        return

    issued: OTPCode | VerificationToken | None = None
    secret = ""
    destination = normalized
    with transaction.atomic():
        current = User.objects.select_for_update().get(pk=user.pk)
        eligible = current.is_active and (
            bool(current.email) and current.is_email_verified
            if by_email
            else current.is_phone_verified and current.phone == normalized
        )
        if not eligible:
            if not by_email:
                otp.reserve_issuance(normalized)
            return
        _invalidate_recovery(current)
        if by_email:
            assert current.email is not None
            destination = current.email
            secret = f"{current.public_id}.{secrets.token_urlsafe(48)}"
            issued = VerificationToken.objects.create(
                user=current,
                purpose=TokenPurpose.PASSWORD_RESET,
                token_hash=_email_token_digest(secret, current),
                expires_at=timezone.now() + RESET_TTL,
            )
        else:
            result = otp.issue(
                destination=current.phone, purpose=OTPPurpose.PASSWORD_RESET, user=current
            )
            issued, secret = result.record, result.code
    assert issued is not None
    _send_reset(backend, destination=destination, secret=secret, record=issued)


def _new_password(user: User, value: str) -> None:
    passwords.enforce_strength(value, user=user)
    if user.check_password(value):
        raise WeakPassword("Choose a different password.")
    user.set_password(value)
    user.save(update_fields=["password"])


def _changed(user: User) -> None:
    _invalidate_recovery(user)
    sessions.revoke_all_for_user(user)
    lockout.clear(user)


def confirm_reset(
    *,
    new_password: str,
    source_ip: str | None,
    phone: str | None = None,
    code: str | None = None,
    reset_token: str | None = None,
) -> None:
    _limit("password_reset_confirm_ip", source_ip or "unknown")
    failure: InvalidOTP | OTPExpired | OTPNotFound | None = None
    with transaction.atomic():
        if reset_token is not None:
            try:
                public_id = uuid.UUID(reset_token.split(".", 1)[0])
            except (ValueError, AttributeError):
                raise InvalidOTP() from None
            user = User.objects.select_for_update().filter(public_id=public_id).first()
            if user is None or not user.is_active or not user.is_email_verified or not user.email:
                raise InvalidOTP()
            token = (
                VerificationToken.objects.select_for_update()
                .filter(
                    user=user,
                    purpose=TokenPurpose.PASSWORD_RESET,
                    token_hash=_email_token_digest(reset_token, user),
                    consumed_at__isnull=True,
                    expires_at__gt=timezone.now(),
                )
                .first()
            )
            if token is None:
                raise InvalidOTP()
            _new_password(user, new_password)
            _changed(user)
        else:
            if phone is None or code is None:
                raise InvalidOTP()
            destination = phones.normalise_phone(phone)
            user = User.objects.select_for_update().filter(phone=destination).first()
            if user is None or not user.is_active or not user.is_phone_verified:
                raise InvalidOTP()
            try:
                verified = otp.verify(
                    destination=destination, purpose=OTPPurpose.PASSWORD_RESET, code=code
                )
            except (InvalidOTP, OTPExpired, OTPNotFound) as exc:
                failure = exc
            else:
                if verified.user_id != user.pk:
                    raise InvalidOTP()
                _new_password(user, new_password)
                _changed(user)
    if failure is not None:
        raise failure
    logger.info("user.password_reset", extra={"user_public_id": str(user.public_id)})


def _current_user(user: User) -> User:
    current = User.objects.select_for_update().filter(pk=user.pk).first()
    if current is None or not current.is_active:
        raise InvalidCredentials()
    return current


def _password_proof(user: User, password: str) -> bool:
    if user.is_locked:
        return False
    if not user.check_password(password):
        lockout.record_failure(user)
        return False
    return True


def change_password(*, user: User, current_password: str, new_password: str) -> None:
    _limit("credential_change", str(user.public_id))
    failure = False
    with transaction.atomic():
        current = _current_user(user)
        if not _password_proof(current, current_password):
            failure = True
        else:
            _new_password(current, new_password)
            _changed(current)
    if failure:
        raise InvalidCredentials()
    logger.info("user.password_changed", extra={"user_public_id": str(current.public_id)})


def _record_pin(user: User, *, was_set: bool) -> None:
    record_audit(
        action="user.pin_changed" if was_set else "user.pin_set",
        actor_type=ActorType.USER,
        actor_user_id=user.pk,
        object_type="accounts.user",
        object_id=str(user.public_id),
        previous_value={"pin_set": was_set},
        new_value={"pin_set": True},
    )


def set_pin(*, user: User, current_password: str, pin: str) -> None:
    _limit("credential_change", str(user.public_id))
    failure = False
    with transaction.atomic():
        current = _current_user(user)
        if not _password_proof(current, current_password):
            failure = True
        else:
            if current.pin_hash:
                raise InvalidPin("The transaction PIN is already set. Use PIN change.")
            current.set_pin(pin)
            current.save(update_fields=["pin_hash"])
            _changed(current)
            _record_pin(current, was_set=False)
    if failure:
        raise InvalidCredentials()


def change_pin(*, user: User, current_pin: str, new_pin: str) -> None:
    _limit("pin_entry", str(user.public_id))
    failure = False
    with transaction.atomic():
        current = _current_user(user)
        if current.is_locked:
            failure = True
        elif not current.check_pin(current_pin):
            lockout.record_failure(current)
            failure = True
        else:
            if current_pin == new_pin:
                raise InvalidPin("Choose a different transaction PIN.")
            current.set_pin(new_pin)
            current.save(update_fields=["pin_hash"])
            _changed(current)
            _record_pin(current, was_set=True)
    if failure:
        raise InvalidPin()
