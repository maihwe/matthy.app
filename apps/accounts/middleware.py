"""Require a current MFA proof for privileged browser cookies on every request."""

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from apps.accounts.constants import SessionRevocationReason
from apps.accounts.services import browser, mfa


class BrowserMFAMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if browser.SESSION_MARKER in request.session or (
            request.user.is_authenticated and mfa.required(request.user)
        ):
            if browser.authenticated(request, touch=True) is None:
                browser.end(request, reason=SessionRevocationReason.REVOKED)
        return self.get_response(request)
