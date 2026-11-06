"""The first S4 slice: password login, refresh, logout and device revocation."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from apps.accounts.constants import DevicePlatform
from apps.accounts.models import Device, User
from apps.accounts.services import credentials, mfa, sessions, tokens
from apps.accounts.services.errors import (
    InvalidCredentials,
    InvalidRefreshToken,
    RateLimited,
)
from config import ratelimit

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    public_id: uuid.UUID | None = None
    fingerprint: str | None = None
    platform: str | None = None
    model: str | None = None
    os_version: str | None = None
    app_version: str | None = None


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str = field(repr=False)
    issued: sessions.IssuedSession


def _limit(scope: str, source_ip: str | None) -> None:
    decision = ratelimit.consume(scope, source_ip or "unknown")
    if not decision.allowed:
        raise RateLimited(scope, decision.retry_after)


def _requires_mfa(user: User) -> bool:
    return mfa.required(user)


def _device_for(user: User, info: DeviceInfo) -> Device:
    """Caller holds the user lock; a supplied device UUID is always owner-scoped."""
    if info.public_id is not None:
        device = Device.objects.filter(user=user, public_id=info.public_id).first()
        if device is None:
            raise InvalidCredentials()
    else:
        device, _ = Device.objects.get_or_create(
            user=user,
            device_fingerprint=info.fingerprint or str(uuid.uuid4()),
            defaults={"platform": info.platform or DevicePlatform.WEB},
        )
    if device.is_revoked:
        raise InvalidCredentials()
    changed = ["last_seen_at"]
    device.last_seen_at = timezone.now()
    for name in ("platform", "model", "os_version", "app_version"):
        value = getattr(info, name)
        if value is not None:
            setattr(device, name, value)
            changed.append(name)
    device.save(update_fields=changed)
    return device


def login(
    *,
    identifier: str,
    password: str,
    source_ip: str | None,
    user_agent: str | None = None,
    device: DeviceInfo | None = None,
) -> TokenPair | mfa.LoginChallenge:
    _limit("login_ip", source_ip)
    verified = credentials.authenticate(
        identifier=identifier, password=password, source_ip=source_ip
    )
    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=verified.pk).first()
        if user is None or not user.is_active or user.password != verified.password:
            raise InvalidCredentials()
        registered_device = _device_for(user, device or DeviceInfo())
        if _requires_mfa(user):
            return mfa.issue_challenge(user, registered_device)
        issued = sessions.issue(
            user=user,
            device=registered_device,
            ip_address=source_ip,
            user_agent=user_agent,
        )
        access = tokens.issue_access(issued.session)
    return TokenPair(access_token=access, issued=issued)


def refresh(
    *, refresh_token: str, source_ip: str | None, user_agent: str | None = None
) -> TokenPair:
    _limit("refresh", source_ip)
    failure: InvalidRefreshToken | None = None
    issued: sessions.IssuedSession | None = None
    access = ""
    with transaction.atomic():
        try:
            issued = sessions.rotate(
                refresh_token=refresh_token, ip_address=source_ip, user_agent=user_agent
            )
        except InvalidRefreshToken as exc:
            # Catch inside the outer transaction. Re-raising here would roll
            # back the inner service's replay revocation (SEC-JWT-004).
            failure = exc
        else:
            if issued.session.mfa_version != issued.session.user.mfa_version or (
                _requires_mfa(issued.session.user)
                and (not issued.session.mfa_verified or not issued.session.user.two_factor_enabled)
            ):
                sessions.revoke_session_family(issued.session)
                failure = InvalidRefreshToken()
            else:
                # A signing failure rolls rotation back: the client still
                # holds its old token and can retry when signing is restored.
                access = tokens.issue_access(issued.session)
    if failure is not None:
        raise failure
    assert issued is not None
    return TokenPair(access_token=access, issued=issued)


def revoke_device(device: Device) -> None:
    """Mark the device and end all its login families under the same user lock."""
    with transaction.atomic():
        User.objects.select_for_update().get(pk=device.user_id)
        Device.objects.filter(pk=device.pk, user_id=device.user_id).update(is_revoked=True)
        sessions.revoke_device_sessions(device)
