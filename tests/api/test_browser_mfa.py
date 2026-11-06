"""Admin MFA journeys, cookie isolation, CSRF and revocation boundaries."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.sessions.backends.cached_db import SessionStore
from django.db import DatabaseError
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.constants import AccountStatus, SessionKind
from apps.accounts.models import (
    Device,
    MFAChallenge,
    MFAEnrollment,
    MFARecoveryCode,
    User,
    UserSession,
)
from apps.accounts.services import browser, mfa, sessions, tokens
from apps.accounts.services.errors import InvalidCredentials, InvalidRefreshToken
from config import ratelimit
from config.phone_verification import DeliveryUnavailable
from tests.api.test_authentication_api import AUTH, _keys
from tests.api.test_mfa_api import _code, _sms_code
from tests.factories import UserFactory
from tests.factories.accounts import FACTORY_PASSWORD

pytestmark = [pytest.mark.api, pytest.mark.security, pytest.mark.django_db]


@pytest.fixture(scope="module")
def keypair():
    return _keys()


@pytest.fixture(autouse=True)
def security_settings(monkeypatch, keypair):
    monkeypatch.setattr(settings, "JWT_SIGNING_KEY", keypair[0])
    monkeypatch.setattr(settings, "JWT_VERIFYING_KEY", keypair[1])
    monkeypatch.setattr(settings, "MFA_ENCRYPTION_KEYS", Fernet.generate_key().decode())


@pytest.fixture
def client():
    return APIClient(
        enforce_csrf_checks=True, REMOTE_ADDR=f"2001:db8::{uuid.uuid4().int % 65535:x}"
    )


def _staff(method="TOTP"):
    user = UserFactory(
        is_staff=True, is_phone_verified=True, phone=f"+234803{uuid.uuid4().int % 10**7:07d}"
    )
    setup = mfa.begin(user=user, method=method, current_password=FACTORY_PASSWORD)
    code = _code(setup["secret"], -1) if method == "TOTP" else _sms_code()
    codes = mfa.confirm(user=user, code=code)
    user.refresh_from_db()
    return user, setup.get("secret"), codes


def _submit(client, url, **body):
    if url.startswith(AUTH):
        return client.post(
            url,
            body,
            format="json",
            HTTP_X_CSRFTOKEN=client.cookies[settings.CSRF_COOKIE_NAME].value,
        )
    body["csrfmiddlewaretoken"] = client.cookies[settings.CSRF_COOKIE_NAME].value
    return client.post(url, body)


def _password(client, user, **body):
    assert client.get("/admin/login/").status_code == 200
    return _submit(client, "/admin/login/", username=user.phone, password=FACTORY_PASSWORD, **body)


def _finish(client, code):
    return _submit(client, "/admin/mfa/", code=code)


def _signed_in(client):
    user, seed, codes = _staff()
    assert _password(client, user).status_code == 302
    assert _finish(client, codes[0]).status_code == 302
    return user, seed, codes, UserSession.objects.get(user=user, kind=SessionKind.BROWSER)


def test_password_then_totp_rotates_cookie_and_csrf_without_browser_tokens(client):
    user, seed, _ = _staff()
    anonymous = client.session.session_key
    assert _password(client, user, next="/admin/accounts/user/").status_code == 302
    pending_key = client.session.session_key
    csrf = client.cookies[settings.CSRF_COOKIE_NAME].value
    assert pending_key != anonymous and "_auth_user_id" not in client.session
    challenge = MFAChallenge.objects.get(user=user, consumed_at__isnull=True)
    assert challenge.audience == SessionKind.BROWSER
    assert challenge.browser_session_key_hash == mfa.digest(pending_key, "browser")
    page = client.get("/admin/mfa/")
    assert page.status_code == 200 and "no-store" in page["Cache-Control"]
    assert client.session[browser.PENDING_TOKEN].encode() not in page.content
    assert b"access_token" not in page.content and b"refresh_token" not in page.content
    assert b"<script>" not in page.content and b"<style>" not in page.content
    response = _finish(client, _code(seed))
    assert response.status_code == 302 and response["Location"] == "/admin/accounts/user/"
    assert client.session.session_key != pending_key
    assert client.cookies[settings.CSRF_COOKIE_NAME].value != csrf
    assert client.session["_auth_user_id"] == str(user.pk)
    assert browser.PENDING_TOKEN not in client.session
    row = UserSession.objects.get(user=user)
    assert row.kind == SessionKind.BROWSER and row.mfa_verified
    assert row.browser_session_key_hash == mfa.digest(client.session.session_key, "browser")
    assert row.browser_credential_state == browser.credential_state(user)
    assert row.device.platform == "WEB"
    assert (row.expires_at - row.issued_at).total_seconds() == 43200
    assert client.get("/admin/").status_code == 200
    response = client.get(f"{AUTH}sessions/")
    assert response.status_code == 200
    assert response.json()["data"][0]["kind"] == "BROWSER"


@pytest.mark.parametrize("method", ["TOTP", "SMS"])
def test_staff_browser_enrollment_displays_codes_once_and_requires_fresh_login(client, method):
    user = UserFactory(
        is_staff=True, is_phone_verified=True, phone=f"+234803{uuid.uuid4().int % 10**7:07d}"
    )
    assert _password(client, user).status_code == 302
    assert _finish(client, "000000").status_code == 401
    page = client.get("/admin/mfa/")
    assert b"MFA is required" in page.content
    response = _submit(
        client, "/admin/mfa/", action="setup", method=method, current_password=FACTORY_PASSWORD
    )
    assert response.status_code == 302
    page = client.get("/admin/mfa/")
    enrollment = MFAEnrollment.objects.get(user=user)
    if method == "TOTP":
        seed = mfa.decrypt(enrollment.secret)
        assert seed.encode() in page.content and b"otpauth://totp/" in page.content
        code = _code(seed, -1)
    else:
        assert b"sent to your verified phone" in page.content
        code = _sms_code()
    response = _submit(client, "/admin/mfa/", action="confirm", code=code)
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    codes = response.context_data["recovery_codes"]
    assert len(codes) == 10 and all(c.encode() in response.content for c in codes)
    assert "_auth_user_id" not in client.session and browser.PENDING_TOKEN not in client.session
    assert not UserSession.objects.filter(user=user).exists()
    assert client.get("/admin/mfa/").status_code == 302
    assert _password(client, user).status_code == 302
    assert _finish(client, codes[0]).status_code == 302
    assert MFARecoveryCode.objects.get(code_hash=mfa.digest(codes[0], "recovery")).consumed_at


def test_sms_resend_replaces_code_but_preserves_password_expiry(client):
    user, _, _ = _staff("SMS")
    assert _password(client, user).status_code == 302
    previous = MFAChallenge.objects.get(user=user, consumed_at__isnull=True)
    old_code = _sms_code()
    assert _submit(client, "/admin/mfa/", action="resend").status_code == 302
    current = MFAChallenge.objects.get(user=user, consumed_at__isnull=True)
    previous.refresh_from_db()
    assert previous.consumed_at and current.pk != previous.pk
    assert current.expires_at == previous.expires_at
    code = _sms_code()
    if old_code != code:
        assert _finish(client, old_code).status_code == 400
    assert _finish(client, code).status_code == 302


def test_sms_delivery_failure_allows_recovery_code(client, monkeypatch):
    user, _, codes = _staff("SMS")

    def unavailable():
        raise DeliveryUnavailable()

    monkeypatch.setattr(mfa, "get_backend", unavailable)
    assert _password(client, user).status_code == 302
    assert b"SMS delivery is unavailable" in client.get("/admin/mfa/").content
    assert _finish(client, codes[0]).status_code == 302


@pytest.mark.parametrize("reason", ["wrong-password", "nonstaff", "unknown", "suspended"])
def test_password_stage_denies_unauthorized_staff_access(client, reason):
    user = UserFactory(is_staff=reason != "nonstaff")
    if reason == "suspended":
        User.objects.filter(pk=user.pk).update(account_status=AccountStatus.SUSPENDED)
    client.get("/admin/login/")
    response = _submit(
        client,
        "/admin/login/",
        username="unknown@example.test" if reason == "unknown" else user.phone,
        password="wrong" if reason == "wrong-password" else FACTORY_PASSWORD,
    )
    assert response.status_code in (400, 401)
    assert "_auth_user_id" not in client.session
    assert not MFAChallenge.objects.filter(user=user).exists()


def test_wrong_mfa_guesses_commit_and_exhausted_proof_cannot_succeed(client):
    user, seed, codes = _staff()
    _password(client, user)
    wrong = next(
        f"{n:06d}" for n in range(10) if f"{n:06d}" not in [_code(seed, off) for off in (-1, 0, 1)]
    )
    for n in range(1, 4):
        response = _finish(client, wrong)
        assert response.status_code == 400
        assert wrong.encode() not in response.content
        assert MFAChallenge.objects.get(user=user).attempts == n
    assert _finish(client, codes[0]).status_code == 302
    assert not UserSession.objects.filter(user=user).exists()
    assert not MFARecoveryCode.objects.filter(consumed_at__isnull=False).exists()


@pytest.mark.parametrize(
    "change", ["expiry", "password", "pin", "role", "device", "version", "phone", "cancel"]
)
def test_pending_proof_dies_after_expiry_cancellation_or_bound_state_change(client, change):
    user, _, codes = _staff()
    _password(client, user)
    row = MFAChallenge.objects.get(user=user)
    if change == "expiry":
        MFAChallenge.objects.filter(pk=row.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
    elif change == "device":
        Device.objects.filter(pk=row.device_id).update(is_revoked=True)
    elif change == "cancel":
        assert _submit(client, "/admin/mfa/", action="restart").status_code == 302
    else:
        updates = {
            "password": {"password": "!changed"},
            "pin": {"pin_hash": "!changed"},
            "role": {"is_staff": False},
            "version": {"mfa_version": 99},
            "phone": {"is_phone_verified": False},
        }
        User.objects.filter(pk=user.pk).update(**updates[change])
    assert _finish(client, codes[0]).status_code == 302
    assert not UserSession.objects.filter(user=user).exists()


def test_browser_and_mobile_proofs_are_not_interchangeable(client):
    user, _, codes = _staff()
    _password(client, user)
    token = client.session[browser.PENDING_TOKEN]
    mobile = APIClient(REMOTE_ADDR=f"2001:db8::{uuid.uuid4().int % 65535:x}")
    assert (
        mobile.post(
            f"{AUTH}mfa/challenge/", {"mfa_token": token, "code": codes[0]}, format="json"
        ).status_code
        == 401
    )
    assert (
        mobile.post(
            f"{AUTH}mfa/enable/",
            {"mfa_token": token, "method": "TOTP", "current_password": FACTORY_PASSWORD},
            format="json",
        ).status_code
        == 401
    )
    response = mobile.post(
        f"{AUTH}login/", {"identifier": user.phone, "password": FACTORY_PASSWORD}, format="json"
    )
    mobile_token = response.json()["data"]["mfa_token"]
    cookie = client.session
    cookie[browser.PENDING_TOKEN] = mobile_token
    cookie.save()
    assert _finish(client, codes[0]).status_code == 302
    assert not UserSession.objects.filter(user=user).exists()
    assert (
        mobile.post(
            f"{AUTH}mfa/challenge/", {"mfa_token": mobile_token, "code": codes[0]}, format="json"
        ).status_code
        == 200
    )


def test_pending_proof_cannot_be_transplanted_to_another_cookie(client):
    user, _, codes = _staff()
    _password(client, user)
    other = APIClient(enforce_csrf_checks=True)
    other.get("/admin/login/")
    cookie = other.session
    cookie.update(dict(client.session.items()))
    cookie.save()
    assert _finish(other, codes[0]).status_code == 302
    assert _finish(client, codes[0]).status_code == 302


def test_authenticated_session_data_transplant_does_not_revoke_original_cookie(client):
    _, _, _, row = _signed_in(client)
    other = APIClient()
    cookie = other.session
    cookie.update(dict(client.session.items()))
    cookie.save()
    assert other.get("/admin/").status_code == 302
    row.refresh_from_db()
    assert row.revoked_at is None
    assert client.get("/admin/").status_code == 200


@pytest.mark.parametrize(
    "change",
    [
        "session",
        "device",
        "password",
        "pin",
        "role",
        "superuser",
        "factor",
        "version",
        "phone",
        "status",
        "idle",
        "absolute",
    ],
)
def test_admin_and_api_cookie_access_end_after_revocation_or_state_change(client, change):
    user, _, _, row = _signed_in(client)
    if change == "session":
        sessions.revoke_session_family(row)
    elif change == "device":
        Device.objects.filter(pk=row.device_id).update(is_revoked=True)
    elif change == "idle":
        UserSession.objects.filter(pk=row.pk).update(
            last_used_at=timezone.now() - timedelta(minutes=31)
        )
    elif change == "absolute":
        UserSession.objects.filter(pk=row.pk).update(
            issued_at=timezone.now() - timedelta(hours=13),
            expires_at=timezone.now() - timedelta(seconds=1),
        )
    else:
        updates = {
            "password": {"password": "!changed"},
            "pin": {"pin_hash": "!changed"},
            "role": {"is_staff": False},
            "superuser": {"is_superuser": True},
            "factor": {"two_factor_enabled": False},
            "version": {"mfa_version": 99},
            "phone": {"is_phone_verified": False},
            "status": {"account_status": AccountStatus.SUSPENDED},
        }
        User.objects.filter(pk=user.pk).update(**updates[change])
    assert client.get("/admin/").status_code == 302
    assert client.get(f"{AUTH}sessions/").status_code == 401
    assert "_auth_user_id" not in client.session


def test_activity_updates_are_bounded_by_absolute_cookie_lifetime(client):
    _, _, _, row = _signed_in(client)
    before = timezone.now() - timedelta(minutes=5)
    UserSession.objects.filter(pk=row.pk).update(
        last_used_at=before, expires_at=timezone.now() + timedelta(seconds=90)
    )
    response = client.get("/admin/")
    assert response.status_code == 200
    assert 0 < int(response.cookies[settings.SESSION_COOKIE_NAME]["max-age"]) <= 90
    row.refresh_from_db()
    row.device.refresh_from_db()
    assert row.last_used_at > before and row.device.last_seen_at > before


@pytest.mark.parametrize("url", ["/admin/logout/", f"{AUTH}logout/"])
def test_logout_requires_post_csrf_and_revokes_backing_browser_session(client, url):
    _, _, _, row = _signed_in(client)
    assert client.get(url).status_code == 405
    assert (
        client.post(url, {}, format="json" if url.startswith(AUTH) else "multipart").status_code
        == 403
    )
    row.refresh_from_db()
    assert row.revoked_at is None
    assert _submit(client, url).status_code in (200, 204)
    row.refresh_from_db()
    assert row.revoked_at and row.revoked_reason == "LOGOUT"
    assert client.get("/admin/").status_code == 302


def test_all_browser_stages_enforce_csrf_and_reject_other_methods(client):
    user, _, codes = _staff()
    assert (
        client.post(
            "/admin/login/", {"username": user.phone, "password": FACTORY_PASSWORD}
        ).status_code
        == 403
    )
    assert client.put("/admin/login/").status_code == 403
    _password(client, user)
    assert client.post("/admin/mfa/", {"code": codes[0]}).status_code == 403
    assert client.get(f"{AUTH}sessions/").status_code == 401
    assert _finish(client, codes[0]).status_code == 302
    assert client.post(f"{AUTH}logout/", {}, format="json").status_code == 403


def test_browser_row_cannot_authorize_bearer_or_refresh_tokens(client, monkeypatch):
    known_secret = "x" * 64
    monkeypatch.setattr(sessions, "_new_token", lambda: known_secret)
    user, _, _, row = _signed_in(client)
    mobile = APIClient()
    mobile.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens.issue_access(row)}")
    assert mobile.get(f"{AUTH}sessions/").status_code == 401
    with pytest.raises(InvalidRefreshToken):
        sessions.rotate(refresh_token=known_secret)
    row.refresh_from_db()
    assert row.revoked_at is None and user.is_staff


def test_browser_login_does_not_require_jwt_keys(client, monkeypatch):
    user, _, codes = _staff()
    monkeypatch.setattr(settings, "JWT_SIGNING_KEY", "")
    monkeypatch.setattr(settings, "JWT_VERIFYING_KEY", "")
    _password(client, user)
    assert _finish(client, codes[0]).status_code == 302
    assert client.get("/admin/").status_code == 200


@pytest.mark.parametrize("failure", ["session-save", "row-create"])
def test_storage_failure_rolls_back_factor_and_restores_pending_cookie(
    client, monkeypatch, failure
):
    user, _, codes = _staff()
    _password(client, user)
    original_key = client.session.session_key
    original_save = SessionStore.save
    orphan_keys = []

    def fail_save(store, *args, **kwargs):
        if browser.SESSION_MARKER in store:
            original_save(store, *args, **kwargs)
            orphan_keys.append(store.session_key)
            raise DatabaseError("synthetic storage failure")
        return original_save(store, *args, **kwargs)

    def fail_create(*args, **kwargs):
        raise DatabaseError("synthetic row failure")

    with monkeypatch.context() as context:
        if failure == "session-save":
            context.setattr(SessionStore, "save", fail_save)
        else:
            context.setattr(UserSession.objects, "create", fail_create)
        assert _finish(client, codes[0]).status_code == 503
    assert client.session.session_key == original_key
    assert browser.PENDING_TOKEN in client.session and "_auth_user_id" not in client.session
    assert not UserSession.objects.filter(user=user).exists()
    assert MFAChallenge.objects.get(user=user).consumed_at is None
    assert not MFARecoveryCode.objects.filter(consumed_at__isnull=False).exists()
    for key in orphan_keys:
        orphan = APIClient()
        orphan.cookies[settings.SESSION_COOKIE_NAME] = key
        assert orphan.get("/admin/").status_code == 302
    assert _finish(client, codes[0]).status_code == 302


@pytest.mark.parametrize("scope", ["login_ip", "mfa_challenge_ip"])
def test_browser_rate_limits_return_retry_after(client, monkeypatch, scope):
    user, _, codes = _staff()
    if scope == "mfa_challenge_ip":
        _password(client, user)
    else:
        client.get("/admin/login/")

    def consume(name, identifier):
        return ratelimit.Decision(name, name != scope, 10, 0, 37)

    monkeypatch.setattr(ratelimit, "consume", consume)
    response = (
        _submit(client, "/admin/login/", username=user.phone, password=FACTORY_PASSWORD)
        if scope == "login_ip"
        else _finish(client, codes[0])
    )
    assert response.status_code == 429 and response["Retry-After"] == "37"


def test_browser_limiter_outage_fails_closed(client, monkeypatch):
    user, _, codes = _staff()
    _password(client, user)

    def unavailable(scope, identifier):
        raise ratelimit.RateLimiterUnavailable(scope)

    monkeypatch.setattr(ratelimit, "consume", unavailable)
    assert _finish(client, codes[0]).status_code == 503
    assert not UserSession.objects.filter(user=user).exists()


@pytest.mark.parametrize(
    "destination",
    [
        "https://evil.example/",
        "//evil.example/",
        "http://testserver/admin/",
        "\\\\evil.example/",
        "javascript:alert(1)",
    ],
)
def test_redirect_destination_is_same_host_and_https_when_secure(client, destination):
    user, _, codes = _staff()
    client.get("/admin/login/", secure=True)
    response = client.post(
        "/admin/login/",
        {
            "username": user.phone,
            "password": FACTORY_PASSWORD,
            "next": destination,
            "csrfmiddlewaretoken": client.cookies[settings.CSRF_COOKIE_NAME].value,
        },
        secure=True,
        HTTP_ORIGIN="https://testserver",
    )
    assert response.status_code == 302
    response = client.post(
        "/admin/mfa/",
        {"code": codes[0], "csrfmiddlewaretoken": client.cookies[settings.CSRF_COOKIE_NAME].value},
        secure=True,
        HTTP_ORIGIN="https://testserver",
    )
    assert response.status_code == 302 and response["Location"] == "/admin/"


def test_secrets_are_masked_and_identifiers_are_html_escaped(client):
    client.get("/admin/login/")
    response = _submit(
        client,
        "/admin/login/",
        username='<script>alert("hello")</script>',
        password="private-password",
    )
    assert response.status_code == 401
    assert b"private-password" not in response.content
    assert b"<script>alert" not in response.content and b"&lt;script&gt;" in response.content


def test_secure_cookie_attributes(client, monkeypatch):
    monkeypatch.setattr(settings, "SESSION_COOKIE_SECURE", True)
    monkeypatch.setattr(settings, "CSRF_COOKIE_SECURE", True)
    user, _, codes = _staff()
    _password(client, user)
    response = _finish(client, codes[0])
    cookie = response.cookies[settings.SESSION_COOKIE_NAME]
    assert cookie["secure"] and cookie["httponly"] and cookie["samesite"] == "Lax"
    assert int(cookie["max-age"]) <= 1800


def test_direct_cookie_authentication_still_validates_browser_proof(client):
    from rest_framework.exceptions import AuthenticationFailed
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory

    from apps.accounts.authentication import AccountSessionAuthentication

    user = UserFactory(is_staff=True)
    request = APIRequestFactory().get("/")
    request.user = user
    request.session = SessionStore()
    with pytest.raises(AuthenticationFailed):
        AccountSessionAuthentication().authenticate(Request(request))


def test_missing_or_invalid_pending_context_is_denied(client):
    client.get("/admin/login/")
    assert client.get("/admin/mfa/").status_code == 302
    request = client.get("/admin/login/").wsgi_request
    with pytest.raises(InvalidCredentials):
        browser.finish(request, code="000000")


def test_django_secret_key_fallback_keeps_cookie_bound_after_key_rotation(client, monkeypatch):
    _, _, _, row = _signed_in(client)
    original_key = client.session.session_key
    original_secret = settings.SECRET_KEY
    monkeypatch.setattr(settings, "SECRET_KEY", "synthetic-rotated-django-secret")
    monkeypatch.setattr(settings, "SECRET_KEY_FALLBACKS", [original_secret])
    assert client.get("/admin/").status_code == 200
    assert client.session.session_key != original_key
    row.refresh_from_db()
    assert row.browser_session_key_hash == mfa.digest(client.session.session_key, "browser")
    assert client.get("/admin/").status_code == 200


@pytest.mark.parametrize("setting", ["SESSION_COOKIE_AGE", "SESSION_ABSOLUTE_TIMEOUT_SECONDS"])
def test_nonpositive_browser_timeout_fails_without_consuming_factor(client, monkeypatch, setting):
    user, _, codes = _staff()
    _password(client, user)
    monkeypatch.setattr(settings, setting, 0)
    assert _finish(client, codes[0]).status_code == 503
    assert MFAChallenge.objects.get(user=user).consumed_at is None
    assert not MFARecoveryCode.objects.filter(consumed_at__isnull=False).exists()


def test_totp_step_rolls_back_on_browser_session_failure(client, monkeypatch):
    user, seed, _ = _staff()
    _password(client, user)
    previous_step = user.last_totp_step
    original_save = SessionStore.save

    def fail_save(store, *args, **kwargs):
        if browser.SESSION_MARKER in store:
            raise DatabaseError("synthetic storage failure")
        return original_save(store, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(SessionStore, "save", fail_save)
        assert _finish(client, _code(seed)).status_code == 503
    user.refresh_from_db()
    assert user.last_totp_step == previous_step
    assert _finish(client, _code(seed)).status_code == 302


def test_browser_setup_failures_keep_user_unenrolled_and_mask_password(client, monkeypatch):
    user = UserFactory(is_staff=True)
    _password(client, user)
    response = _submit(
        client, "/admin/mfa/", action="setup", method="TOTP", current_password="wrong-secret"
    )
    assert response.status_code == 401 and b"wrong-secret" not in response.content
    assert not MFAEnrollment.objects.filter(user=user).exists()
    monkeypatch.setattr(settings, "MFA_ENCRYPTION_KEYS", "")
    assert (
        _submit(
            client, "/admin/mfa/", action="setup", method="TOTP", current_password=FACTORY_PASSWORD
        ).status_code
        == 503
    )
    assert not MFAEnrollment.objects.filter(user=user).exists()


def test_browser_setup_expiry_returns_to_method_selection(client):
    user = UserFactory(is_staff=True)
    _password(client, user)
    _submit(client, "/admin/mfa/", action="setup", method="TOTP", current_password=FACTORY_PASSWORD)
    MFAEnrollment.objects.filter(user=user).update(expires_at=timezone.now() - timedelta(seconds=1))
    page = client.get("/admin/mfa/")
    assert page.status_code == 200 and b"MFA is required" in page.content
    assert _submit(client, "/admin/mfa/", action="confirm", code="000000").status_code == 400


def test_malformed_marker_is_flushed_without_revoke_of_unrelated_session(client):
    _, _, _, row = _signed_in(client)
    cookie = client.session
    cookie[browser.SESSION_MARKER] = "malformed"
    cookie.save()
    assert client.get("/admin/").status_code == 302
    row.refresh_from_db()
    assert row.revoked_at is None


def test_browser_resend_is_unavailable_for_totp_and_enrollment(client):
    user, _, _ = _staff()
    _password(client, user)
    assert _submit(client, "/admin/mfa/", action="resend").status_code == 401
    _submit(client, "/admin/mfa/", action="restart")
    user = UserFactory(is_staff=True)
    _password(client, user)
    assert _submit(client, "/admin/mfa/", action="resend").status_code == 401


def test_browser_encryption_key_rotation_preserves_cookie_access(client, monkeypatch):
    from django.core.management import call_command

    _, _, _, _ = _signed_in(client)
    old = settings.MFA_ENCRYPTION_KEYS
    new = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "MFA_ENCRYPTION_KEYS", f"{new},{old}")
    call_command("rotate_mfa_keys")
    monkeypatch.setattr(settings, "MFA_ENCRYPTION_KEYS", new)
    assert client.get("/admin/").status_code == 200
