"""MFA browser login and revocable cookies, without browser-held bearer tokens."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.contrib.auth.models import AnonymousUser
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone

from apps.accounts.constants import DevicePlatform, SessionKind, SessionRevocationReason
from apps.accounts.models import Device, MFAChallenge, User, UserSession
from apps.accounts.services import credentials, mfa, sessions
from apps.accounts.services.errors import AuthenticationUnavailable, InvalidCredentials, InvalidOTP
from config.api.throttling import client_ip

PENDING_TOKEN = "browser_mfa_pending"  # noqa: S105 — session field name, not a secret
PENDING_NEXT = "browser_mfa_next"
SESSION_MARKER = "browser_mfa_session"


def credential_state(user: User) -> str:
    """Django validates its signed cookie with secret-key fallback separately.
    This digest binds already-hashed credentials and known privilege/factor state
    without making an encryption/signing-key rotation terminate valid cookies.
    """
    fields = [
        str(user.public_id),
        user.password,
        user.pin_hash,
        user.mfa_version,
        user.two_factor_enabled,
        user.two_factor_method,
        user.is_staff,
        user.is_superuser,
        user.phone,
        user.is_phone_verified,
    ]
    # Existing zero-version cookies retain their pre-S5 digest. Once roles
    # change, their monotonic version makes the legacy digest unusable.
    if user.authorization_version or user.role_mfa_required:
        fields.extend([user.authorization_version, user.role_mfa_required])
    value = json.dumps(fields).encode()
    return hashlib.sha256(b"ihweza.browser-credentials.v1:" + value).hexdigest()


def clear_pending(request: HttpRequest) -> None:
    request.session.pop(PENDING_TOKEN, None)
    request.session.pop(PENDING_NEXT, None)


def end(request: HttpRequest, *, reason: str = SessionRevocationReason.LOGOUT) -> None:
    """Only the row bound to this user's actual cookie may be revoked."""
    marker = request.session.get(SESSION_MARKER)
    key = request.session.session_key
    if request.user.is_authenticated and isinstance(marker, str) and key:
        try:
            public_id = uuid.UUID(marker)
        except ValueError:
            public_id = None
        if public_id is not None:
            with transaction.atomic():
                User.objects.select_for_update().filter(pk=request.user.pk).first()
                row = UserSession.objects.filter(
                    user_id=request.user.pk,
                    public_id=public_id,
                    kind=SessionKind.BROWSER,
                    browser_session_key_hash=mfa.digest(key, "browser"),
                ).first()
                if row is not None:
                    sessions.revoke_session_family(row, reason=reason)
    # Also retire a pending browser proof on cancellation. It cannot be moved
    # to another cookie or used through the mobile MFA endpoints.
    token = request.session.get(PENDING_TOKEN)
    if isinstance(token, str) and key:
        pending_challenge = MFAChallenge.objects.filter(
            token_hash=mfa.digest(token, "challenge"),
            audience=SessionKind.BROWSER,
            browser_session_key_hash=mfa.digest(key, "browser"),
        ).first()
        if pending_challenge is not None:
            with transaction.atomic():
                User.objects.select_for_update().filter(pk=pending_challenge.user_id).first()
                MFAChallenge.objects.filter(
                    pk=pending_challenge.pk, consumed_at__isnull=True
                ).update(consumed_at=timezone.now())
    django_logout(request)


def begin_login(request: HttpRequest, *, identifier: str, password: str, next_url: str) -> None:
    mfa.limit("login_ip", client_ip(request) or "unknown")
    verified = credentials.authenticate(
        identifier=identifier, password=password, source_ip=client_ip(request)
    )
    if not verified.is_staff:
        raise InvalidCredentials()
    end(request)
    request.session.create()
    key = request.session.session_key
    assert key is not None
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=verified.pk)
        if not user.is_active or not user.is_staff or user.password != verified.password:
            raise InvalidCredentials()
        device = Device.objects.create(
            user=user, device_fingerprint=str(uuid.uuid4()), platform=DevicePlatform.WEB
        )
        challenge = mfa.issue_challenge(user, device, browser_session_key=key)
    request.session[PENDING_TOKEN] = challenge.token
    request.session[PENDING_NEXT] = next_url
    request.session.set_expiry(int(mfa.CHALLENGE_TTL.total_seconds()))


def pending(request: HttpRequest) -> tuple[User, MFAChallenge]:
    token = request.session.get(PENDING_TOKEN)
    key = request.session.session_key
    if not isinstance(token, str) or not key:
        raise InvalidCredentials()
    user, challenge = mfa.challenge_user(token, browser_session_key=key)
    if not user.is_staff:
        raise InvalidCredentials()
    return user, challenge


def resend(request: HttpRequest) -> None:
    token = request.session.get(PENDING_TOKEN)
    key = request.session.session_key
    user, previous = pending(request)
    assert isinstance(token, str) and key
    if previous.enrollment_required or user.two_factor_method != "SMS":
        raise InvalidCredentials()
    mfa.limit("mfa_challenge_ip", client_ip(request) or "unknown")
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        mfa._live_challenge(user, token, browser_session_key=key)
        challenge = mfa.issue_challenge(user, previous.device, browser_session_key=key)
        # Resending must not extend the original password proof's lifetime.
        MFAChallenge.objects.filter(token_hash=mfa.digest(challenge.token, "challenge")).update(
            expires_at=previous.expires_at
        )
    request.session[PENDING_TOKEN] = challenge.token


def finish(request: HttpRequest, *, code: str) -> UserSession:
    """Factor use, login rotation and the backing row commit together.
    Catch bad guesses inside the outer transaction to retain their counters.
    """
    token = request.session.get(PENDING_TOKEN)
    key = request.session.session_key
    if not isinstance(token, str) or not key:
        raise InvalidCredentials()
    old_session = request.session
    snapshot = dict(old_session.items())
    failure: InvalidOTP | None = None
    row = None
    try:
        with transaction.atomic():
            try:
                user, challenge = mfa.verify_challenge(
                    mfa_token=token,
                    code=code,
                    source_ip=client_ip(request),
                    browser_session_key=key,
                )
            except InvalidOTP as exc:
                failure = exc
            else:
                if not user.is_staff:
                    raise InvalidCredentials()
                if (
                    settings.SESSION_ABSOLUTE_TIMEOUT_SECONDS <= 0
                    or settings.SESSION_COOKIE_AGE <= 0
                ):
                    raise AuthenticationUnavailable()
                django_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
                new_key = request.session.session_key
                assert new_key is not None
                stamp = timezone.now()
                row = UserSession.objects.create(
                    user=user,
                    device=challenge.device,
                    kind=SessionKind.BROWSER,
                    refresh_token_hash=sessions._digest(sessions._new_token()),
                    mfa_verified=True,
                    mfa_version=user.mfa_version,
                    authorization_version=user.authorization_version,
                    browser_session_key_hash=mfa.digest(new_key, "browser"),
                    browser_credential_state=credential_state(user),
                    issued_at=stamp,
                    last_used_at=stamp,
                    expires_at=stamp + timedelta(seconds=settings.SESSION_ABSOLUTE_TIMEOUT_SECONDS),
                    ip_address=client_ip(request),
                    user_agent=request.META.get("HTTP_USER_AGENT", "")[:512],
                )
                clear_pending(request)
                request.session[SESSION_MARKER] = str(row.public_id)
                request.session.set_expiry(
                    min(settings.SESSION_COOKIE_AGE, settings.SESSION_ABSOLUTE_TIMEOUT_SECONDS)
                )
                # Persist inside atomic, before publishing an authenticated
                # cookie. Any cached orphan still fails the DB proof check.
                request.session.save()
    except Exception:
        # Django may already have cycled the in-memory key. Restore the original
        # anonymous context after rollback so the same proof can be retried.
        restored = type(old_session)(session_key=key)
        restored.update(snapshot)
        request.session = restored
        request.user = AnonymousUser()
        raise
    if failure is not None:
        raise failure
    assert row is not None
    return row


def authenticated(request: HttpRequest, *, touch: bool = False) -> UserSession | None:
    marker = request.session.get(SESSION_MARKER)
    key = request.session.session_key
    if not request.user.is_authenticated or not isinstance(marker, str) or not key:
        return None
    # Resolving Django's lazy user can rotate the key when SECRET_KEY_FALLBACKS
    # validates an old auth hash. Validate the original binding before moving it
    # to the key Django just generated; copied session data still fails below.
    current_key = request.session.session_key
    try:
        public_id = uuid.UUID(marker)
    except ValueError:
        return None
    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=request.user.pk).first()
        if user is None or not user.is_active or not user.is_staff or not user.two_factor_enabled:
            return None
        row = (
            UserSession.objects.select_related("device")
            .filter(
                user=user,
                public_id=public_id,
                kind=SessionKind.BROWSER,
            )
            .first()
        )
        stamp = timezone.now()
        if (
            row is None
            or row.revoked_at is not None
            or row.expires_at <= stamp
            or row.mfa_version != user.mfa_version
            or row.authorization_version != user.authorization_version
            or not row.mfa_verified
            or not hmac.compare_digest(row.browser_session_key_hash, mfa.digest(key, "browser"))
            or not hmac.compare_digest(row.browser_credential_state, credential_state(user))
            or row.device is None
            or row.device.user_id != user.pk
            or row.device.is_revoked
            or (row.last_used_at or row.issued_at) + timedelta(seconds=settings.SESSION_COOKIE_AGE)
            <= stamp
        ):
            return None
        if touch:
            row.last_used_at = stamp
            assert current_key is not None
            row.browser_session_key_hash = mfa.digest(current_key, "browser")
            row.save(update_fields=["last_used_at", "browser_session_key_hash"])
            Device.objects.filter(pk=row.device.pk).update(last_seen_at=stamp)
            request.user = user
            remaining = max(1, int((row.expires_at - stamp).total_seconds()))
            request.session.set_expiry(min(settings.SESSION_COOKIE_AGE, remaining))
    return row
