"""Password then MFA sign-in for the revocable, server-side admin cookie."""

from __future__ import annotations

from typing import Any

from django.contrib.admin import AdminSite
from django.db import DatabaseError
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import URLPattern, URLResolver, path, reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters

from apps.accounts.forms import (
    BrowserCodeForm,
    BrowserConfirmForm,
    BrowserLoginForm,
    BrowserSetupForm,
)
from apps.accounts.models import MFAEnrollment, User
from apps.accounts.services import browser, mfa
from apps.accounts.services.errors import (
    AccountError,
    AuthenticationUnavailable,
    InvalidCredentials,
    RateLimited,
)
from config.ratelimit import RateLimiterUnavailable


class MFAAdminSite(AdminSite):
    def get_urls(self) -> list[URLPattern | URLResolver]:
        return [path("mfa/", self.mfa_login, name="mfa"), *super().get_urls()]

    def has_permission(self, request: HttpRequest) -> bool:
        return browser.authenticated(request) is not None

    def _url(self, name: str) -> str:
        return reverse(f"{self.name}:{name}")

    def _next(self, request: HttpRequest, value: Any) -> str:
        if isinstance(value, str) and url_has_allowed_host_and_scheme(
            value,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return value
        return self._url("index")

    def _render(
        self, request: HttpRequest, template: str, context: dict[str, Any], *, status: int = 200
    ) -> TemplateResponse:
        request.current_app = self.name
        response = TemplateResponse(
            request,
            template,
            {
                **self.each_context(request),
                "is_nav_sidebar_enabled": False,
                "login_url": self._url("login"),
                "mfa_url": self._url("mfa"),
                **context,
            },
            status=status,
        )
        response["Cache-Control"] = "no-store"
        return response

    def _error(self, error: Exception) -> tuple[str, int]:
        if isinstance(error, RateLimited):
            return RateLimited.message, 429
        if isinstance(error, (AuthenticationUnavailable, RateLimiterUnavailable, DatabaseError)):
            return AuthenticationUnavailable.message, 503
        if isinstance(error, InvalidCredentials):
            return "Sign-in could not be completed. Check your credentials or start again.", 401
        assert isinstance(error, AccountError)
        return error.detail, 400

    @method_decorator(never_cache)
    @method_decorator(csrf_protect)
    @method_decorator(sensitive_post_parameters("password"))
    def login(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        if request.method not in ("GET", "POST"):
            return HttpResponseNotAllowed(["GET", "POST"])
        destination = self._next(request, request.POST.get("next", request.GET.get("next")))
        if request.method == "GET":
            if self.has_permission(request):
                return HttpResponseRedirect(destination)
            if browser.PENDING_TOKEN in request.session:
                try:
                    browser.pending(request)
                except AccountError:
                    browser.end(request)
                else:
                    return HttpResponseRedirect(self._url("mfa"))
        form = BrowserLoginForm(request.POST if request.method == "POST" else None)
        error = None
        status = 200
        if request.method == "POST" and form.is_valid():
            try:
                browser.begin_login(
                    request,
                    identifier=form.cleaned_data["username"],
                    password=form.cleaned_data["password"],
                    next_url=destination,
                )
            except (AccountError, RateLimiterUnavailable, DatabaseError) as exc:
                error = exc
                message, status = self._error(exc)
                form.add_error(None, message)
            else:
                return HttpResponseRedirect(self._url("mfa"))
        response = self._render(
            request,
            "admin/browser_login.html",
            {"title": "Sign in", "form": form, "next": destination},
            status=status,
        )
        if isinstance(error, RateLimited):
            response["Retry-After"] = str(error.retry_after)
        return response

    def _enrollment(self, user: User) -> dict[str, str] | None:
        enrollment = MFAEnrollment.objects.filter(user=user).first()
        if (
            enrollment is None
            or enrollment.expires_at <= timezone.now()
            or enrollment.attempts >= mfa.MAX_ATTEMPTS
            or enrollment.credential_state != mfa.state(user)
        ):
            return None
        data = {"method": enrollment.method}
        if enrollment.method == "TOTP":
            seed = mfa.decrypt(enrollment.secret)
            data.update(secret=seed, provisioning_uri=mfa.provisioning_uri(user, seed))
        return data

    @method_decorator(never_cache)
    @method_decorator(csrf_protect)
    @method_decorator(sensitive_post_parameters("current_password", "code"))
    def mfa_login(self, request: HttpRequest) -> HttpResponse:
        if request.method not in ("GET", "POST"):
            return HttpResponseNotAllowed(["GET", "POST"])
        if request.method == "POST" and request.POST.get("action") == "restart":
            browser.end(request)
            return HttpResponseRedirect(self._url("login"))
        try:
            user, challenge = browser.pending(request)
        except AccountError:
            browser.end(request)
            return HttpResponseRedirect(self._url("login"))
        setup = challenge.enrollment_required
        form: BrowserCodeForm | BrowserConfirmForm | BrowserSetupForm = BrowserCodeForm()
        enrollment = None
        error = None
        error_message = None
        status = 200
        try:
            if setup:
                enrollment = self._enrollment(user)
                form = BrowserConfirmForm() if enrollment else BrowserSetupForm()
            if request.method == "POST":
                action = request.POST.get("action", "verify")
                if setup and action == "setup":
                    form = BrowserSetupForm(request.POST)
                    if form.is_valid():
                        mfa.begin(
                            user=user,
                            method=form.cleaned_data["method"],
                            current_password=form.cleaned_data["current_password"],
                            mfa_token=request.session[browser.PENDING_TOKEN],
                            browser_session_key=request.session.session_key,
                        )
                        return HttpResponseRedirect(self._url("mfa"))
                elif setup and action == "confirm":
                    form = BrowserConfirmForm(request.POST)
                    if form.is_valid():
                        codes = mfa.confirm(
                            user=user,
                            code=form.cleaned_data["code"],
                            mfa_token=request.session[browser.PENDING_TOKEN],
                            browser_session_key=request.session.session_key,
                        )
                        browser.end(request)
                        return self._render(
                            request,
                            "admin/browser_recovery.html",
                            {"title": "Save your recovery codes", "recovery_codes": codes},
                        )
                elif not setup and action == "resend":
                    browser.resend(request)
                    return HttpResponseRedirect(self._url("mfa"))
                elif not setup and action == "verify":
                    form = BrowserCodeForm(request.POST)
                    if form.is_valid():
                        destination = self._next(request, request.session.get(browser.PENDING_NEXT))
                        browser.finish(request, code=form.cleaned_data["code"])
                        return HttpResponseRedirect(destination)
                else:
                    raise InvalidCredentials()
        except (AccountError, RateLimiterUnavailable, DatabaseError) as exc:
            error = exc
            message, status = self._error(exc)
            error_message = message
        response = self._render(
            request,
            "admin/browser_mfa.html",
            {
                "title": "Set up MFA" if setup else "Verify your sign-in",
                "form": form,
                "error_message": error_message,
                "setup": setup,
                "enrollment": enrollment,
                "method": user.two_factor_method,
                "sms_unavailable": not setup
                and user.two_factor_method == "SMS"
                and not challenge.code_hash,
            },
            status=status,
        )
        if isinstance(error, RateLimited):
            response["Retry-After"] = str(error.retry_after)
        return response

    @method_decorator(never_cache)
    @method_decorator(csrf_protect)
    # The admin stub only allows TemplateResponse; the method also returns HTTP 405.
    def logout(  # type: ignore[override]
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        browser.end(request)
        return super().logout(request, extra_context)
