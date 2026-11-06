"""S4 login and session endpoints, using the shared error and pagination contracts."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import cast

from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, PolymorphicProxySerializer, extend_schema
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.constants import SessionRevocationReason
from apps.accounts.models import Device, User, UserSession
from apps.accounts.serializers import (
    DeviceListSerializer,
    DeviceSerializer,
    EmailVerificationRequestSerializer,
    EmailVerificationSerializer,
    EmailVerifiedResponseSerializer,
    EmailVerifiedSerializer,
    ErrorResponseSerializer,
    LoginSerializer,
    MessageResponseSerializer,
    MFAChallengeSerializer,
    MFAConfirmSerializer,
    MFADisableSerializer,
    MFAEnableSerializer,
    MFAEnrollmentResponseSerializer,
    MFALoginResponseSerializer,
    MFARecoveryResponseSerializer,
    PasswordChangeSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    PhoneCodeRequestSerializer,
    PhoneVerificationSerializer,
    PhoneVerifiedResponseSerializer,
    PhoneVerifiedSerializer,
    PinChangeSerializer,
    PinSetSerializer,
    RefreshSerializer,
    RegistrationSerializer,
    SessionListSerializer,
    SessionSerializer,
    TokenResponseSerializer,
    UserIdentitySerializer,
)
from apps.accounts.services import (
    authentication,
    browser,
    email_verification,
    mfa,
    onboarding,
    recovery,
    sessions,
)
from apps.accounts.services.errors import (
    AccountClosed,
    AccountError,
    AccountSuspended,
    AuthenticationUnavailable,
    InvalidAccessToken,
    InvalidCredentials,
    InvalidOTP,
    InvalidRefreshToken,
    OTPExpired,
    OTPNotFound,
    RateLimited,
)
from config.api.exceptions import APIError
from config.api.pagination import CursorPagination
from config.api.throttling import client_ip
from config.ratelimit import RateLimiterUnavailable

PAGINATION_PARAMETERS = [
    OpenApiParameter("cursor", str, description="The pagination cursor value."),
    OpenApiParameter(
        "limit",
        {"type": "integer", "maximum": CursorPagination.max_page_size},
        default=CursorPagination.page_size,
        description="Number of results per page.",
    ),
]


class _PublicAccountError(APIError):
    auth_header: str | None = None
    wait: int | None = None

    def __init__(self, status: int, code: str, message: str, wait: int | None = None) -> None:
        self.status_code = status
        self.api_code = code
        self.wait = wait
        if status == 401:
            self.auth_header = "Bearer"
        super().__init__(message)


class AccountAPIView(APIView):
    permission_classes: Sequence[type[BasePermission]] = (IsAuthenticated,)

    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, RateLimited):
            exc = _PublicAccountError(429, "rate_limited", RateLimited.message, exc.retry_after)
        elif isinstance(exc, RateLimiterUnavailable | AuthenticationUnavailable):
            exc = _PublicAccountError(503, "service_unavailable", AuthenticationUnavailable.message)
        elif isinstance(exc, InvalidCredentials | InvalidRefreshToken | InvalidAccessToken):
            exc = _PublicAccountError(401, "authentication_required", exc.message)
        elif isinstance(exc, AccountClosed | AccountSuspended):
            exc = _PublicAccountError(403, exc.code, exc.message)
        elif isinstance(exc, InvalidOTP | OTPExpired | OTPNotFound):
            exc = _PublicAccountError(
                400, "validation_error", "The verification code is invalid or expired."
            )
        elif isinstance(exc, AccountError):
            exc = _PublicAccountError(400, "validation_error", str(exc))
        return super().handle_exception(exc)


VERIFICATION_REQUEST_MESSAGE = "If this phone number needs verification, a code will be sent."


class EmailVerificationRequestView(AccountAPIView):
    @extend_schema(
        tags=["auth"],
        request=EmailVerificationRequestSerializer,
        responses={
            200: MessageResponseSerializer,
            400: ErrorResponseSerializer,
            401: ErrorResponseSerializer,
            429: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
    )
    def post(self, request: Request) -> Response:
        serializer = EmailVerificationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email_verification.request_verification(
            user=cast(User, request.user), source_ip=client_ip(request)
        )
        response = Response(
            {
                "data": {
                    "message": "If your account has an email needing verification, a token will be sent."
                }
            }
        )
        response["Cache-Control"] = "no-store"
        return response


class EmailVerificationView(AccountAPIView):
    permission_classes = (AllowAny,)
    authentication_classes = ()

    @extend_schema(
        tags=["auth"],
        request=EmailVerificationSerializer,
        responses={
            200: EmailVerifiedResponseSerializer,
            400: ErrorResponseSerializer,
            429: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
    )
    def post(self, request: Request) -> Response:
        serializer = EmailVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = email_verification.verify(**serializer.validated_data, source_ip=client_ip(request))
        response = Response({"data": EmailVerifiedSerializer(user).data})
        response["Cache-Control"] = "no-store"
        return response


class PasswordResetRequestView(AccountAPIView):
    permission_classes = (AllowAny,)
    authentication_classes = ()

    @extend_schema(
        tags=["auth"],
        request=PasswordResetRequestSerializer,
        responses={
            200: MessageResponseSerializer,
            400: ErrorResponseSerializer,
            429: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
    )
    def post(self, request: Request) -> Response:
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        recovery.request_reset(**serializer.validated_data, source_ip=client_ip(request))
        return Response(
            {
                "data": {
                    "message": "If a verified recovery channel is available, reset instructions will be sent."
                }
            }
        )


class PasswordResetConfirmView(AccountAPIView):
    permission_classes = (AllowAny,)
    authentication_classes = ()

    @extend_schema(
        tags=["auth"],
        request=PasswordResetConfirmSerializer,
        responses={
            204: None,
            400: ErrorResponseSerializer,
            429: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
    )
    def post(self, request: Request) -> Response:
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        recovery.confirm_reset(**serializer.validated_data, source_ip=client_ip(request))
        return Response(status=204)


def _clear_browser_session(request: Request) -> None:
    if not isinstance(request.auth, dict):
        browser.end(request._request)


class PasswordChangeView(AccountAPIView):
    @extend_schema(
        tags=["auth"],
        request=PasswordChangeSerializer,
        responses={
            204: None,
            400: ErrorResponseSerializer,
            401: ErrorResponseSerializer,
            403: ErrorResponseSerializer,
            429: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
    )
    def post(self, request: Request) -> Response:
        serializer = PasswordChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        recovery.change_password(user=cast(User, request.user), **serializer.validated_data)
        _clear_browser_session(request)
        return Response(status=204)


class PinSetView(AccountAPIView):
    @extend_schema(
        tags=["auth"],
        request=PinSetSerializer,
        responses={
            204: None,
            400: ErrorResponseSerializer,
            401: ErrorResponseSerializer,
            403: ErrorResponseSerializer,
            429: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
    )
    def post(self, request: Request) -> Response:
        serializer = PinSetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        recovery.set_pin(user=cast(User, request.user), **serializer.validated_data)
        _clear_browser_session(request)
        return Response(status=204)


class PinChangeView(AccountAPIView):
    @extend_schema(
        tags=["auth"],
        request=PinChangeSerializer,
        responses={
            204: None,
            400: ErrorResponseSerializer,
            401: ErrorResponseSerializer,
            403: ErrorResponseSerializer,
            429: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
    )
    def post(self, request: Request) -> Response:
        serializer = PinChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        recovery.change_pin(user=cast(User, request.user), **serializer.validated_data)
        _clear_browser_session(request)
        return Response(status=204)


class RegistrationView(AccountAPIView):
    permission_classes = (AllowAny,)
    authentication_classes = ()

    @extend_schema(
        tags=["auth"],
        request=RegistrationSerializer,
        responses={
            201: MessageResponseSerializer,
            400: ErrorResponseSerializer,
            429: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
    )
    def post(self, request: Request) -> Response:
        serializer = RegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        onboarding.register(**serializer.validated_data, source_ip=client_ip(request))
        return Response({"data": {"message": VERIFICATION_REQUEST_MESSAGE}}, status=201)


class PhoneCodeRequestView(AccountAPIView):
    permission_classes = (AllowAny,)
    authentication_classes = ()

    @extend_schema(
        tags=["auth"],
        request=PhoneCodeRequestSerializer,
        responses={
            200: MessageResponseSerializer,
            400: ErrorResponseSerializer,
            429: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
    )
    def post(self, request: Request) -> Response:
        serializer = PhoneCodeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        onboarding.request_phone_code(
            phone=serializer.validated_data["phone"], source_ip=client_ip(request)
        )
        return Response({"data": {"message": VERIFICATION_REQUEST_MESSAGE}})


class PhoneVerificationView(AccountAPIView):
    permission_classes = (AllowAny,)
    authentication_classes = ()

    @extend_schema(
        tags=["auth"],
        request=PhoneVerificationSerializer,
        responses={
            200: PhoneVerifiedResponseSerializer,
            400: ErrorResponseSerializer,
            429: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
    )
    def post(self, request: Request) -> Response:
        serializer = PhoneVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = onboarding.verify_phone(**serializer.validated_data, source_ip=client_ip(request))
        return Response({"data": PhoneVerifiedSerializer(user).data})


def _pair_response(pair: authentication.TokenPair) -> Response:
    session = pair.issued.session
    assert session.device is not None
    response = Response(
        {
            "data": {
                "access_token": pair.access_token,
                "refresh_token": pair.issued.refresh_token,
                "expires_in": int(sessions.ACCESS_TTL.total_seconds()),
                "device_id": str(session.device.public_id),
                "user": UserIdentitySerializer(session.user).data,
            }
        }
    )
    response["Cache-Control"] = "no-store"
    return response


def _setup_identity(request: Request, token: str | None) -> User:
    if token is not None:
        user = mfa.enrollment_user(token)
        if request.user.is_authenticated and request.user.pk != user.pk:
            raise InvalidCredentials()
        return user
    if not request.user.is_authenticated:
        raise InvalidCredentials()
    return request.user


class MFAEnableView(AccountAPIView):
    permission_classes = (AllowAny,)

    @extend_schema(
        tags=["auth"],
        request=MFAEnableSerializer,
        responses={
            200: MFAEnrollmentResponseSerializer,
            400: ErrorResponseSerializer,
            401: ErrorResponseSerializer,
            429: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
    )
    def post(self, request: Request) -> Response:
        mfa.limit("mfa_challenge_ip", client_ip(request) or "unknown")
        serializer = MFAEnableSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        body = serializer.validated_data
        user = _setup_identity(request, body.get("mfa_token"))
        response = Response({"data": mfa.begin(user=user, **body)})
        response["Cache-Control"] = "no-store"
        return response


class MFAConfirmView(AccountAPIView):
    permission_classes = (AllowAny,)

    @extend_schema(
        tags=["auth"],
        request=MFAConfirmSerializer,
        responses={
            200: MFARecoveryResponseSerializer,
            400: ErrorResponseSerializer,
            401: ErrorResponseSerializer,
            429: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
    )
    def post(self, request: Request) -> Response:
        mfa.limit("mfa_challenge_ip", client_ip(request) or "unknown")
        serializer = MFAConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        body = serializer.validated_data
        user = _setup_identity(request, body.get("mfa_token"))
        codes = mfa.confirm(user=user, **body)
        _clear_browser_session(request)
        response = Response({"data": {"recovery_codes": codes}})
        response["Cache-Control"] = "no-store"
        return response


class MFADisableView(AccountAPIView):
    @extend_schema(
        tags=["auth"],
        request=MFADisableSerializer,
        responses={
            204: None,
            400: ErrorResponseSerializer,
            401: ErrorResponseSerializer,
            429: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
    )
    def post(self, request: Request) -> Response:
        serializer = MFADisableSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mfa.disable(user=cast(User, request.user), **serializer.validated_data)
        _clear_browser_session(request)
        return Response(status=204)


class MFAChallengeView(AccountAPIView):
    permission_classes = (AllowAny,)
    authentication_classes = ()

    @extend_schema(
        tags=["auth"],
        request=MFAChallengeSerializer,
        responses={
            200: TokenResponseSerializer,
            400: ErrorResponseSerializer,
            401: ErrorResponseSerializer,
            429: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
    )
    def post(self, request: Request) -> Response:
        serializer = MFAChallengeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        access, issued = mfa.complete(
            **serializer.validated_data,
            source_ip=client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT"),
        )
        return _pair_response(authentication.TokenPair(access, issued))


class LoginView(AccountAPIView):
    permission_classes = (AllowAny,)
    authentication_classes = ()

    @extend_schema(
        tags=["auth"],
        request=LoginSerializer,
        responses={
            200: PolymorphicProxySerializer(
                component_name="LoginResponse",
                serializers=[TokenResponseSerializer, MFALoginResponseSerializer],
                resource_type_field_name=None,
            ),
            400: ErrorResponseSerializer,
            401: ErrorResponseSerializer,
            403: ErrorResponseSerializer,
            429: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
    )
    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        body = serializer.validated_data
        device = body.get("device", {})
        pair = authentication.login(
            identifier=body["identifier"],
            password=body["password"],
            source_ip=client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT"),
            device=authentication.DeviceInfo(
                public_id=device.get("id"),
                fingerprint=device.get("fingerprint"),
                platform=device.get("platform"),
                model=device.get("model"),
                os_version=device.get("os_version"),
                app_version=device.get("app_version"),
            ),
        )
        if isinstance(pair, mfa.LoginChallenge):
            response = Response(
                {
                    "data": {
                        "mfa_required": True,
                        "mfa_token": pair.token,
                        "enrollment_required": pair.enrollment_required,
                        "method": pair.method,
                        "expires_in": 300,
                        "sms_sent": pair.sms_sent,
                    }
                }
            )
            response["Cache-Control"] = "no-store"
            return response
        return _pair_response(pair)


class RefreshView(AccountAPIView):
    permission_classes = (AllowAny,)
    authentication_classes = ()

    @extend_schema(
        tags=["auth"],
        request=RefreshSerializer,
        responses={
            200: TokenResponseSerializer,
            400: ErrorResponseSerializer,
            401: ErrorResponseSerializer,
            429: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
    )
    def post(self, request: Request) -> Response:
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        pair = authentication.refresh(
            refresh_token=serializer.validated_data["refresh_token"],
            source_ip=client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT"),
        )
        return _pair_response(pair)


class LogoutView(AccountAPIView):
    permission_classes = (IsAuthenticated,)

    @extend_schema(tags=["auth"], request=None, responses={204: None})
    def post(self, request: Request) -> Response:
        user = cast(User, request.user)
        if isinstance(request.auth, dict):
            session = get_object_or_404(UserSession, user=user, public_id=request.auth["sid"])
            sessions.revoke_session_family(session, reason=SessionRevocationReason.LOGOUT)
        else:
            # Django SessionAuthentication already enforced CSRF for this path.
            browser.end(request._request)
        return Response(status=204)


class SessionListView(AccountAPIView):
    permission_classes = (IsAuthenticated,)
    ordering = ("-issued_at", "public_id")

    @extend_schema(
        tags=["auth"],
        parameters=PAGINATION_PARAMETERS,
        responses=SessionListSerializer,
    )
    def get(self, request: Request) -> Response:
        rows = (
            UserSession.objects.filter(
                user=cast(User, request.user),
                revoked_at__isnull=True,
                expires_at__gt=timezone.now(),
            )
            .exclude(device__is_revoked=True)
            .select_related("device")
        )
        pager = CursorPagination()
        page = pager.paginate_queryset(rows, request, view=self)
        return pager.get_paginated_response(SessionSerializer(page, many=True).data)


class SessionRevokeView(AccountAPIView):
    permission_classes = (IsAuthenticated,)

    @extend_schema(tags=["auth"], responses={204: None, 404: ErrorResponseSerializer})
    def delete(self, request: Request, public_id: uuid.UUID) -> Response:
        session = get_object_or_404(UserSession, user=request.user, public_id=public_id)
        sessions.revoke_session_family(session)
        return Response(status=204)


class DeviceListView(AccountAPIView):
    permission_classes = (IsAuthenticated,)
    ordering = ("-created_at", "public_id")

    @extend_schema(
        tags=["auth"],
        parameters=PAGINATION_PARAMETERS,
        responses=DeviceListSerializer,
    )
    def get(self, request: Request) -> Response:
        rows = Device.objects.filter(user=cast(User, request.user), is_revoked=False)
        pager = CursorPagination()
        page = pager.paginate_queryset(rows, request, view=self)
        return pager.get_paginated_response(DeviceSerializer(page, many=True).data)


class DeviceRevokeView(AccountAPIView):
    permission_classes = (IsAuthenticated,)

    @extend_schema(tags=["auth"], responses={204: None, 404: ErrorResponseSerializer})
    def delete(self, request: Request, public_id: uuid.UUID) -> Response:
        device = get_object_or_404(Device, user=request.user, public_id=public_id)
        authentication.revoke_device(device)
        return Response(status=204)
