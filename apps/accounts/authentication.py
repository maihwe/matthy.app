"""Bearer authentication backed by the session and device the server issued."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from django.utils import timezone
from drf_spectacular.authentication import SessionScheme
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.authentication import (
    BaseAuthentication,
    SessionAuthentication,
    get_authorization_header,
)
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.request import Request

from apps.accounts.constants import SessionKind, SessionRevocationReason
from apps.accounts.models import Device, User, UserSession
from apps.accounts.services import browser
from apps.accounts.services.errors import InvalidAccessToken
from apps.accounts.services.mfa import required
from apps.accounts.services.tokens import decode_access

if TYPE_CHECKING:
    from drf_spectacular.openapi import AutoSchema


class SessionJWTAuthentication(BaseAuthentication):
    """A valid signature alone cannot survive logout, account or device revocation."""

    def authenticate(self, request: Request) -> tuple[User, dict[str, Any]] | None:
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() != b"bearer":
            return None
        if len(parts) != 2:
            raise AuthenticationFailed(InvalidAccessToken.message)
        try:
            claims = decode_access(parts[1].decode("utf-8"))
            user_id = uuid.UUID(claims["sub"])
            session_id = uuid.UUID(claims["sid"])
            user = User.objects.filter(public_id=user_id).first()
            if user is None or not user.is_active:
                raise InvalidAccessToken()
            session = (
                UserSession.objects.select_related("device")
                .filter(public_id=session_id, user=user)
                .first()
            )
            stamp = timezone.now()
            if (
                session is None
                or session.kind != SessionKind.TOKEN
                or session.mfa_version != user.mfa_version
                or session.authorization_version != user.authorization_version
                or (required(user) and (not session.mfa_verified or not user.two_factor_enabled))
                or session.expires_at <= stamp
                or session.device is None
                or session.device.user_id != user.pk
                or session.device.is_revoked
            ):
                raise InvalidAccessToken()
            if session.revoked_at is not None and (
                session.revoked_reason != SessionRevocationReason.ROTATED
                or not UserSession.objects.filter(
                    user=user,
                    family_id=session.family_id,
                    device=session.device,
                    revoked_at__isnull=True,
                    expires_at__gt=stamp,
                ).exists()
            ):
                raise InvalidAccessToken()
        except (InvalidAccessToken, UnicodeError, ValueError, TypeError, AttributeError):
            raise AuthenticationFailed(InvalidAccessToken.message) from None

        # ATOMIC_REQUESTS is false: these single-statement activity updates
        # commit before a view takes the user lock for a lifecycle operation.
        UserSession.objects.filter(pk=session.pk).update(last_used_at=stamp)
        Device.objects.filter(pk=session.device.pk).update(last_seen_at=stamp)
        return user, claims

    def authenticate_header(self, request: Request) -> str:
        return "Bearer"


class AccountSessionAuthentication(SessionAuthentication):
    """Password-only Django cookies cannot bypass the API's second factor."""

    def authenticate(self, request: Request) -> tuple[User, None] | None:
        result = super().authenticate(request)
        if (
            result is not None
            and required(result[0])
            and browser.authenticated(request._request) is None
        ):
            raise AuthenticationFailed("Complete MFA login to use the API.")
        return result


class AccountSessionAuthenticationScheme(SessionScheme):  # type: ignore[no-untyped-call]
    target_class = "apps.accounts.authentication.AccountSessionAuthentication"


class SessionJWTAuthenticationScheme(OpenApiAuthenticationExtension):  # type: ignore[no-untyped-call]
    target_class = "apps.accounts.authentication.SessionJWTAuthentication"
    name = "jwtAuth"

    def get_security_definition(self, auto_schema: AutoSchema) -> dict[str, Any]:
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
