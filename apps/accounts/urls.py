"""S4 session and phone onboarding routes; other auth flows remain pending."""

from django.urls import path

from apps.accounts import views

app_name = "auth"

urlpatterns = [
    path("register/", views.RegistrationView.as_view(), name="register"),
    path("otp/request/", views.PhoneCodeRequestView.as_view(), name="request-phone-code"),
    path("phone/verify/", views.PhoneVerificationView.as_view(), name="verify-phone"),
    path(
        "email/request/",
        views.EmailVerificationRequestView.as_view(),
        name="request-email-verification",
    ),
    path("email/verify/", views.EmailVerificationView.as_view(), name="verify-email"),
    path("password/reset/", views.PasswordResetRequestView.as_view(), name="password-reset"),
    path(
        "password/reset/confirm/",
        views.PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path("password/change/", views.PasswordChangeView.as_view(), name="password-change"),
    path("pin/set/", views.PinSetView.as_view(), name="pin-set"),
    path("pin/change/", views.PinChangeView.as_view(), name="pin-change"),
    path("mfa/enable/", views.MFAEnableView.as_view(), name="mfa-enable"),
    path("mfa/confirm/", views.MFAConfirmView.as_view(), name="mfa-confirm"),
    path("mfa/disable/", views.MFADisableView.as_view(), name="mfa-disable"),
    path("mfa/challenge/", views.MFAChallengeView.as_view(), name="mfa-challenge"),
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("token/refresh/", views.RefreshView.as_view(), name="refresh"),
    path("sessions/", views.SessionListView.as_view(), name="sessions"),
    path("sessions/<uuid:public_id>/", views.SessionRevokeView.as_view(), name="revoke-session"),
    path("devices/", views.DeviceListView.as_view(), name="devices"),
    path("devices/<uuid:public_id>/", views.DeviceRevokeView.as_view(), name="revoke-device"),
]
