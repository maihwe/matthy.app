"""Confirmed TOTP/SMS enrollment and single-use, password-bound MFA proofs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import struct
from dataclasses import dataclass, field
from datetime import timedelta
from smtplib import SMTPException
from urllib.parse import quote, urlencode

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.accounts.constants import SessionKind, TwoFactorMethod
from apps.accounts.models import Device, MFAChallenge, MFAEnrollment, MFARecoveryCode, User
from apps.accounts.services import otp, sessions, tokens
from apps.accounts.services.errors import (
    AccountError,
    AuthenticationUnavailable,
    InvalidCredentials,
    InvalidOTP,
    RateLimited,
)
from config import ratelimit
from config.phone_verification import DeliveryUnavailable, get_backend

logger = logging.getLogger(__name__)
CHALLENGE_TTL = timedelta(minutes=5)
ENROLLMENT_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class LoginChallenge:
    token: str = field(repr=False)
    enrollment_required: bool
    method: str | None
    sms_sent: bool | None = None


def limit(scope: str, identifier: str) -> None:
    decision = ratelimit.consume(scope, identifier)
    if not decision.allowed:
        raise RateLimited(scope, decision.retry_after)


def required(user: User) -> bool:
    return user.two_factor_enabled or user.is_staff or user.is_superuser or user.role_mfa_required


def state(user: User) -> str:
    """Invalidate proofs after password/PIN, factor, role or phone changes."""
    value = (
        f"{user.get_session_auth_hash()}:{user.two_factor_enabled}:"
        f"{user.two_factor_method}:{user.is_staff}:{user.is_superuser}:"
        f"{user.phone}:{user.is_phone_verified}"
    )
    if user.authorization_version or user.role_mfa_required:
        value += f":{user.authorization_version}:{user.role_mfa_required}"
    return salted_hmac("ihweza.mfa.state.v1", value, algorithm="sha256").hexdigest()


def digest(value: str, purpose: str) -> str:
    return hashlib.sha256(f"ihweza.mfa.{purpose}.v1:{value}".encode()).hexdigest()


def cipher() -> MultiFernet:
    """First key encrypts; retained keys decrypt during controlled rotation."""
    try:
        keys = settings.MFA_ENCRYPTION_KEYS.split(",")
        if not all(keys):
            raise ValueError("MFA keyring is empty.")
        return MultiFernet([Fernet(key.strip().encode()) for key in keys])
    except (ValueError, TypeError, AttributeError):
        raise AuthenticationUnavailable() from None


def encrypt(secret: str) -> str:
    return cipher().encrypt(secret.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return cipher().decrypt(value.encode()).decode()
    except (InvalidToken, UnicodeError):
        raise AuthenticationUnavailable() from None


def totp(secret: str, step: int, digits: int = 6) -> str:
    """RFC 6238, SHA-1, 30-second steps; SHA-1 is the interoperable TOTP PRF."""
    key = base64.b32decode(secret, casefold=True)
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = mac[-1] & 15
    number = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % (10**digits)).zfill(digits)


def _step(secret: str, code: str, last_step: int = -1) -> int | None:
    current = int(timezone.now().timestamp()) // 30
    try:
        matches = [
            step
            for step in (current - 1, current, current + 1)
            if hmac.compare_digest(totp(secret, step), code) and step > last_step
        ]
    except (ValueError, struct.error):
        raise AuthenticationUnavailable() from None
    return max(matches) if matches else None


def _sms(user: User) -> tuple[str, str]:
    if not user.is_phone_verified:
        raise AccountError("Verify your phone before enabling SMS MFA.")
    try:
        backend = get_backend()
        otp.reserve_issuance(user.phone)
        code = f"{secrets.randbelow(1_000_000):06d}"
        backend.send(destination=user.phone, code=code, purpose="MFA")
    except (DeliveryUnavailable, OSError, SMTPException, ImportError, AttributeError):
        raise AuthenticationUnavailable() from None
    return code, make_password(code)


def issue_challenge(
    user: User, device: Device, *, browser_session_key: str | None = None
) -> LoginChallenge:
    """Caller holds user lock. A fresh login supersedes earlier challenges."""
    enrollment_required = not user.two_factor_enabled
    if not enrollment_required and user.two_factor_method not in (
        TwoFactorMethod.TOTP,
        TwoFactorMethod.SMS,
    ):
        raise AuthenticationUnavailable()
    code_hash = ""
    sms_sent = None
    if not enrollment_required and user.two_factor_method == TwoFactorMethod.SMS:
        sms_sent = False
        try:
            if user.is_phone_verified:
                _, code_hash = _sms(user)
                sms_sent = True
        except AuthenticationUnavailable:
            # A member with no working phone channel must still be able to use
            # a recovery code. No SMS code is accepted for this challenge.
            logger.warning("mfa.sms_unavailable", extra={"user_public_id": str(user.public_id)})
    token = secrets.token_urlsafe(48)
    MFAChallenge.objects.filter(user=user, consumed_at__isnull=True).update(
        consumed_at=timezone.now()
    )
    MFAChallenge.objects.create(
        user=user,
        device=device,
        token_hash=digest(token, "challenge"),
        credential_state=state(user),
        enrollment_required=enrollment_required,
        audience=SessionKind.BROWSER if browser_session_key is not None else SessionKind.TOKEN,
        browser_session_key_hash=digest(browser_session_key, "browser")
        if browser_session_key is not None
        else "",
        code_hash=code_hash,
        expires_at=timezone.now() + CHALLENGE_TTL,
    )
    logger.info("mfa.challenge_issued", extra={"user_public_id": str(user.public_id)})
    return LoginChallenge(token, enrollment_required, user.two_factor_method, sms_sent)


def _live_challenge(
    user: User, token: str, *, browser_session_key: str | None = None
) -> MFAChallenge:
    challenge = MFAChallenge.objects.filter(
        user=user, token_hash=digest(token, "challenge")
    ).first()
    if (
        challenge is None
        or challenge.audience
        != (SessionKind.BROWSER if browser_session_key is not None else SessionKind.TOKEN)
        or (
            browser_session_key is not None
            and not hmac.compare_digest(
                challenge.browser_session_key_hash, digest(browser_session_key, "browser")
            )
        )
        or challenge.consumed_at is not None
        or challenge.expires_at <= timezone.now()
        or challenge.attempts >= MAX_ATTEMPTS
        or not hmac.compare_digest(challenge.credential_state, state(user))
        or not user.is_active
        or challenge.device.user_id != user.pk
        or challenge.device.is_revoked
    ):
        raise InvalidCredentials()
    return challenge


def challenge_user(
    token: str, *, browser_session_key: str | None = None
) -> tuple[User, MFAChallenge]:
    row = MFAChallenge.objects.filter(token_hash=digest(token, "challenge")).first()
    if row is None:
        raise InvalidCredentials()
    user = row.user
    return user, _live_challenge(user, token, browser_session_key=browser_session_key)


def enrollment_user(token: str, *, browser_session_key: str | None = None) -> User:
    """Only dedicated setup endpoints accept this restricted password proof."""
    user, challenge = challenge_user(token, browser_session_key=browser_session_key)
    if not challenge.enrollment_required or user.two_factor_enabled:
        raise InvalidCredentials()
    return user


def _setup_user(user: User, token: str | None, browser_session_key: str | None = None) -> User:
    locked = User.objects.select_for_update().get(pk=user.pk)
    if not locked.is_active or state(locked) != state(user):
        raise InvalidCredentials()
    if token is not None:
        challenge = _live_challenge(locked, token, browser_session_key=browser_session_key)
        if not challenge.enrollment_required:
            raise InvalidCredentials()
    if locked.two_factor_enabled:
        raise AccountError("MFA is already enabled.")
    return locked


def provisioning_uri(user: User, seed: str) -> str:
    label = quote(f"IHWEZA:{user.public_id}", safe="")
    query = urlencode({"secret": seed, "issuer": "IHWEZA", "digits": 6, "period": 30})
    return f"otpauth://totp/{label}?{query}"


def begin(
    *,
    user: User,
    method: str,
    current_password: str,
    mfa_token: str | None = None,
    browser_session_key: str | None = None,
) -> dict[str, str | int]:
    limit("mfa_setup", str(user.public_id))
    with transaction.atomic():
        user = _setup_user(user, mfa_token, browser_session_key)
        if not user.check_password(current_password):
            raise InvalidCredentials()
        if method not in (TwoFactorMethod.TOTP, TwoFactorMethod.SMS):
            raise AccountError("Choose TOTP or SMS.")
        response: dict[str, str | int] = {"method": method, "expires_in": 600}
        secret, code_hash = "", ""
        if method == TwoFactorMethod.TOTP:
            seed = base64.b32encode(secrets.token_bytes(20)).decode()
            secret = encrypt(seed)
            response.update(secret=seed, provisioning_uri=provisioning_uri(user, seed))
        else:
            _, code_hash = _sms(user)
        MFAEnrollment.objects.update_or_create(
            user=user,
            defaults={
                "method": method,
                "secret": secret,
                "code_hash": code_hash,
                "credential_state": state(user),
                "expires_at": timezone.now() + ENROLLMENT_TTL,
                "attempts": 0,
            },
        )
    return response


def _recovery_codes(user: User) -> list[str]:
    codes = [secrets.token_hex(10) for _ in range(10)]
    MFARecoveryCode.objects.filter(user=user).delete()
    MFARecoveryCode.objects.bulk_create(
        [MFARecoveryCode(user=user, code_hash=digest(code, "recovery")) for code in codes]
    )
    return codes


def confirm(
    *, user: User, code: str, mfa_token: str | None = None, browser_session_key: str | None = None
) -> list[str]:
    limit("mfa_verify", str(user.public_id))
    failure = False
    codes: list[str] = []
    with transaction.atomic():
        user = _setup_user(user, mfa_token, browser_session_key)
        enrollment = MFAEnrollment.objects.filter(user=user).first()
        if (
            enrollment is None
            or enrollment.expires_at <= timezone.now()
            or enrollment.attempts >= MAX_ATTEMPTS
            or enrollment.credential_state != state(user)
        ):
            raise InvalidOTP()
        step = None
        if enrollment.method == TwoFactorMethod.TOTP:
            step = _step(decrypt(enrollment.secret), code)
            valid = step is not None
        else:
            valid = check_password(code, enrollment.code_hash)
        if not valid:
            enrollment.attempts += 1
            enrollment.save(update_fields=["attempts"])
            failure = True
        else:
            user.two_factor_enabled = True
            user.two_factor_method = enrollment.method
            user.totp_secret = enrollment.secret
            user.last_totp_step = step if step is not None else -1
            user.mfa_version += 1
            user.save(
                update_fields=[
                    "two_factor_enabled",
                    "two_factor_method",
                    "totp_secret",
                    "last_totp_step",
                    "mfa_version",
                ]
            )
            codes = _recovery_codes(user)
            enrollment.delete()
            sessions.revoke_all_for_user(user)
            MFAChallenge.objects.filter(user=user, consumed_at__isnull=True).update(
                consumed_at=timezone.now()
            )
            logger.info("mfa.enabled", extra={"user_public_id": str(user.public_id)})
    if failure:
        raise InvalidOTP()
    return codes


def _verify_factor(user: User, code: str, sms_hash: str = "") -> bool:
    recovery = MFARecoveryCode.objects.filter(
        user=user, code_hash=digest(code, "recovery"), consumed_at__isnull=True
    ).first()
    if recovery is not None:
        recovery.consumed_at = timezone.now()
        recovery.save(update_fields=["consumed_at"])
        return True
    if user.two_factor_method == TwoFactorMethod.TOTP:
        step = _step(decrypt(user.totp_secret), code, user.last_totp_step)
        if step is not None:
            user.last_totp_step = step
            user.save(update_fields=["last_totp_step"])
            return True
    elif user.two_factor_method == TwoFactorMethod.SMS and sms_hash:
        return check_password(code, sms_hash)
    return False


def verify_challenge(
    *, mfa_token: str, code: str, source_ip: str | None, browser_session_key: str | None = None
) -> tuple[User, MFAChallenge]:
    """Consume a second-factor proof. Outer callers catch bad guesses before
    their transaction exits, so counters commit while issuance can roll back.
    """
    limit("mfa_challenge_ip", source_ip or "unknown")
    row = MFAChallenge.objects.filter(token_hash=digest(mfa_token, "challenge")).first()
    if row is None:
        raise InvalidCredentials()
    limit("mfa_verify", str(row.user.public_id))
    failure = False
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=row.user_id)
        challenge = _live_challenge(user, mfa_token, browser_session_key=browser_session_key)
        if challenge.enrollment_required or not user.two_factor_enabled:
            raise InvalidCredentials()
        if not _verify_factor(user, code, challenge.code_hash):
            challenge.attempts += 1
            if challenge.attempts >= MAX_ATTEMPTS:
                challenge.consumed_at = timezone.now()
            challenge.save(update_fields=["attempts", "consumed_at"])
            failure = True
            logger.info("mfa.challenge_failed", extra={"user_public_id": str(user.public_id)})
        else:
            challenge.consumed_at = timezone.now()
            challenge.save(update_fields=["consumed_at"])
            logger.info("mfa.challenge_completed", extra={"user_public_id": str(user.public_id)})
    if failure:
        raise InvalidOTP()
    return user, challenge


def complete(
    *, mfa_token: str, code: str, source_ip: str | None, user_agent: str | None = None
) -> tuple[str, sessions.IssuedSession]:
    failure: InvalidOTP | None = None
    issued = None
    access = ""
    with transaction.atomic():
        try:
            user, challenge = verify_challenge(mfa_token=mfa_token, code=code, source_ip=source_ip)
        except InvalidOTP as exc:
            failure = exc
        else:
            issued = sessions.issue(
                user=user,
                device=challenge.device,
                ip_address=source_ip,
                user_agent=user_agent,
                mfa_verified=True,
            )
            access = tokens.issue_access(issued.session)
    if failure is not None:
        raise failure
    assert issued is not None
    return access, issued


def disable(*, user: User, current_password: str, code: str) -> None:
    limit("mfa_setup", str(user.public_id))
    limit("mfa_verify", str(user.public_id))
    with transaction.atomic():
        locked = User.objects.select_for_update().get(pk=user.pk)
        if (
            not locked.is_active
            or state(locked) != state(user)
            or not locked.check_password(current_password)
        ):
            raise InvalidCredentials()
        if locked.is_staff or locked.is_superuser or locked.role_mfa_required:
            raise AccountError("MFA is mandatory for this account.")
        if not locked.two_factor_enabled or not _verify_factor(locked, code):
            raise InvalidOTP()
        locked.two_factor_enabled = False
        locked.two_factor_method = None
        locked.totp_secret = ""
        locked.last_totp_step = -1
        locked.mfa_version += 1
        locked.save(
            update_fields=[
                "two_factor_enabled",
                "two_factor_method",
                "totp_secret",
                "last_totp_step",
                "mfa_version",
            ]
        )
        MFARecoveryCode.objects.filter(user=locked).delete()
        MFAEnrollment.objects.filter(user=locked).delete()
        MFAChallenge.objects.filter(user=locked, consumed_at__isnull=True).update(
            consumed_at=timezone.now()
        )
        sessions.revoke_all_for_user(locked)
        logger.info("mfa.disabled", extra={"user_public_id": str(locked.public_id)})
