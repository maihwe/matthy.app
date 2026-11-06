"""Email ownership, purpose/credential binding and the recovery integration."""

from __future__ import annotations

import ipaddress
import logging
from datetime import timedelta
from uuid import uuid4

import pytest
from django.conf import settings
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.constants import AccountStatus, TokenPurpose, VerificationStatus
from apps.accounts.models import User, UserProfile, VerificationToken
from apps.accounts.services import email_verification, recovery
from apps.accounts.services.errors import InvalidCredentials
from config import ratelimit
from config.phone_verification import DeliveryUnavailable, LocalEmailCapture
from tests.api.test_authentication_api import AUTH, _bearer, _keys, _login
from tests.api.test_onboarding_api import PASSWORD, _code, _phone, _register, _verify
from tests.factories import UserFactory
from tests.factories.accounts import FACTORY_PASSWORD

pytestmark = [pytest.mark.api, pytest.mark.security, pytest.mark.django_db]


@pytest.fixture(scope="module")
def rsa_keys():
    return _keys()


@pytest.fixture(autouse=True)
def signing_keys(monkeypatch, rsa_keys) -> None:
    monkeypatch.setattr(settings, "JWT_SIGNING_KEY", rsa_keys[0])
    monkeypatch.setattr(settings, "JWT_VERIFYING_KEY", rsa_keys[1])


@pytest.fixture
def client() -> APIClient:
    address = ipaddress.IPv6Address(int(ipaddress.IPv6Address("2001:db8::")) + (uuid4().int >> 64))
    return APIClient(REMOTE_ADDR=str(address))


def _member(**overrides):
    return UserFactory(
        **{
            "email": f"verification-{uuid4().hex}@example.test",
            "is_email_verified": False,
            **overrides,
        }
    )


def _authorize(client, user):
    pair = _login(client, user)
    _bearer(client, pair["access_token"])
    return pair


def _request(client, **body):
    return client.post(f"{AUTH}email/request/", body, format="json")


def _token() -> str:
    return mail.outbox[-1].body.split(" is ", 1)[1].split(". It expires", 1)[0]


def _confirm(client, token):
    return client.post(f"{AUTH}email/verify/", {"token": token}, format="json")


@pytest.mark.parametrize("phone_first", [True, False])
def test_register_verify_both_channels_and_recover_through_email(
    client, phone_first, caplog
) -> None:
    caplog.set_level(logging.INFO, logger=email_verification.__name__)
    phone = _phone()
    email = f"Ada-{uuid4().hex}@Example.test"
    assert _register(client, phone, email=email).status_code == 201
    user = User.objects.get(phone=phone)
    phone_code = _code()
    assert user.email == email and not user.is_email_verified
    if phone_first:
        assert _verify(client, phone, phone_code).status_code == 200
    pair = _login(client, user, password=PASSWORD)
    _bearer(client, pair["access_token"])
    count = len(mail.outbox)
    assert (
        client.post(f"{AUTH}password/reset/", {"identifier": email}, format="json").status_code
        == 200
    )
    assert len(mail.outbox) == count
    requested = _request(client)
    assert requested.status_code == 200 and requested["Cache-Control"] == "no-store"
    token = _token()
    assert len(token) == 101 and mail.outbox[-1].to == [email]
    assert "24 hours" in mail.outbox[-1].body
    record = VerificationToken.objects.get(user=user, purpose=TokenPurpose.EMAIL_VERIFY)
    assert record.token_hash != token and token not in str(list(VerificationToken.objects.values()))
    assert (record.expires_at - record.created_at).total_seconds() == pytest.approx(86400, abs=1)
    assert token not in requested.content.decode()
    client.credentials(HTTP_AUTHORIZATION="Bearer unrelated-invalid-token")
    confirmed = _confirm(client, token)
    assert confirmed.status_code == 200 and confirmed["Cache-Control"] == "no-store"
    assert confirmed.json()["data"] == {
        "id": str(user.public_id),
        "email": email,
        "is_email_verified": True,
        "verification_status": VerificationStatus.FULLY_VERIFIED
        if phone_first
        else VerificationStatus.EMAIL_VERIFIED,
    }
    assert _confirm(client, token).status_code == 400
    if not phone_first:
        assert _verify(client, phone, phone_code).status_code == 200
    user.refresh_from_db()
    record.refresh_from_db()
    assert user.is_email_verified and user.is_phone_verified
    assert user.verification_status == VerificationStatus.FULLY_VERIFIED and record.consumed_at
    # Verifying the saved address changes ownership facts, not credentials.
    _bearer(client, pair["access_token"])
    assert client.get(f"{AUTH}sessions/").status_code == 200
    assert (
        client.post(
            f"{AUTH}password/reset/", {"identifier": email.upper()}, format="json"
        ).status_code
        == 200
    )
    reset_secret = _token()
    reset = client.post(
        f"{AUTH}password/reset/confirm/",
        {"reset_token": reset_secret, "new_password": "rT9!mP4@vL7#xQ2"},
        format="json",
    )
    assert reset.status_code == 204
    user.refresh_from_db()
    assert user.check_password("rT9!mP4@vL7#xQ2")
    assert token not in caplog.text and email not in caplog.text


def test_resend_invalidates_only_prior_email_verification_tokens(client) -> None:
    user = _member()
    _authorize(client, user)
    unrelated = VerificationToken.objects.create(
        user=user,
        purpose=TokenPurpose.PASSWORD_RESET,
        token_hash="unrelated-reset-fixture",
        expires_at=timezone.now() + timedelta(minutes=10),
    )
    assert _request(client).status_code == 200
    first = _token()
    assert _request(client).status_code == 200
    second = _token()
    assert first != second
    assert _confirm(client, first).status_code == 400
    assert _confirm(client, second).status_code == 200
    unrelated.refresh_from_db()
    assert unrelated.consumed_at is None


@pytest.mark.parametrize("state", ["no_email", "verified"])
def test_request_is_generic_when_no_verification_is_needed(client, state) -> None:
    user = _member(email=None) if state == "no_email" else _member(is_email_verified=True)
    _authorize(client, user)
    response = _request(client)
    assert response.status_code == 200
    assert set(response.json()["data"]) == {"message"}
    assert not VerificationToken.objects.exists() and not mail.outbox


def test_delivery_cannot_be_redirected_or_change_an_address(client) -> None:
    user = _member()
    _authorize(client, user)
    assert _request(client, email="another@example.test", user_id=str(uuid4())).status_code == 200
    assert mail.outbox[-1].to == [user.email]
    user.refresh_from_db()
    assert user.email != "another@example.test"


def test_request_requires_authentication(client) -> None:
    assert _request(client).status_code == 401
    assert not VerificationToken.objects.exists()


@pytest.mark.parametrize(
    "change",
    [
        "expired",
        "consumed",
        "purpose",
        "password",
        "pin",
        "mfa",
        "email",
        "suspended",
        "closed",
        "unknown",
        "deleted",
        "forged_owner",
    ],
)
def test_invalid_and_stale_tokens_have_one_public_error(client, change) -> None:
    user = _member()
    _authorize(client, user)
    _request(client)
    token = _token()
    if change == "expired":
        VerificationToken.objects.update(
            created_at=timezone.now() - timedelta(days=2),
            expires_at=timezone.now() - timedelta(seconds=1),
        )
    elif change == "consumed":
        VerificationToken.objects.update(consumed_at=timezone.now())
    elif change == "purpose":
        VerificationToken.objects.update(purpose=TokenPurpose.PASSWORD_RESET)
    elif change == "unknown":
        token = f"{uuid4()}.unknown"
    elif change == "deleted":
        user.delete()
    elif change == "forged_owner":
        token = f"{_member().public_id}.{token.split('.', 1)[1]}"
    else:
        values = {
            "password": {"password": "!changed"},
            "pin": {"pin_hash": "!changed"},
            "mfa": {"mfa_version": 19},
            "email": {"email": f"changed-{uuid4().hex}@example.test"},
            "suspended": {"account_status": AccountStatus.SUSPENDED},
            "closed": {"account_status": AccountStatus.CLOSED},
        }
        User.objects.filter(pk=user.pk).update(**values[change])
    response = _confirm(client, token)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["message"] == "The verification code is invalid or expired."
    assert not User.objects.filter(pk=user.pk, is_email_verified=True).exists()


@pytest.mark.parametrize("token", ["bad-token", "", None, "x" * 129])
def test_malformed_tokens_use_validation_errors(client, token) -> None:
    assert _confirm(client, token).status_code == 400


def test_verification_token_cannot_reset_password(client) -> None:
    user = _member()
    _authorize(client, user)
    _request(client)
    token = _token()
    assert _confirm(client, token).status_code == 200
    response = client.post(
        f"{AUTH}password/reset/confirm/",
        {"reset_token": token, "new_password": "rT9!mP4@vL7#xQ2"},
        format="json",
    )
    assert response.status_code == 400
    user.refresh_from_db()
    assert user.check_password(FACTORY_PASSWORD)


def test_actual_password_reset_token_cannot_verify_email(client) -> None:
    user = _member(is_email_verified=True)
    recovery.request_reset(identifier=user.email, source_ip="2001:db8::3")
    token = _token()
    User.objects.filter(pk=user.pk).update(is_email_verified=False)
    assert _confirm(client, token).status_code == 400


def test_undelivered_token_is_consumed_and_resend_can_recover(client, monkeypatch) -> None:
    user = _member()
    _authorize(client, user)
    with monkeypatch.context() as context:

        def unavailable(self, **kwargs):
            raise DeliveryUnavailable()

        context.setattr(LocalEmailCapture, "send", unavailable)
        assert _request(client).status_code == 503
    record = VerificationToken.objects.get()
    assert record.consumed_at is not None
    assert _request(client).status_code == 200
    assert _confirm(client, _token()).status_code == 200


@pytest.mark.parametrize(
    "path", ["", "missing.backend", "config.phone_verification.DeliveryUnavailable"]
)
def test_unavailable_backend_fails_before_issuing_a_token(client, monkeypatch, path) -> None:
    user = _member()
    _authorize(client, user)
    monkeypatch.setattr(settings, "PHONE_VERIFICATION_BACKEND", path)
    assert _request(client).status_code == 503
    assert not VerificationToken.objects.exists()


@pytest.mark.parametrize(
    "scope",
    [
        "email_verification_ip",
        "email_verification",
        "email_verification_destination",
        "email_verification_confirm_ip",
    ],
)
def test_rate_limits_return_retry_after(client, monkeypatch, scope) -> None:
    user = _member()
    _authorize(client, user)

    def consume(name, identifier):
        return ratelimit.Decision(name, name != scope, 10, 0, 37)

    monkeypatch.setattr(ratelimit, "consume", consume)
    response = _confirm(client, "unknown") if scope.endswith("confirm_ip") else _request(client)
    assert response.status_code == 429 and response["Retry-After"] == "37"
    assert not VerificationToken.objects.exists()


def test_real_request_budget_allows_three_then_refuses(client) -> None:
    user = _member()
    _authorize(client, user)
    assert [_request(client).status_code for _ in range(4)] == [200, 200, 200, 429]
    assert len(mail.outbox) == 3
    assert VerificationToken.objects.filter(consumed_at__isnull=True).count() == 1


def test_redis_outage_fails_closed_for_request_and_confirmation(client, monkeypatch) -> None:
    user = _member()
    _authorize(client, user)

    def unavailable(*args, **kwargs):
        raise ratelimit.RateLimiterUnavailable("email_verification")

    monkeypatch.setattr(ratelimit, "consume", unavailable)
    assert _request(client).status_code == 503
    assert _confirm(client, "unknown").status_code == 503


def test_cookie_request_requires_csrf() -> None:
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(_member())
    assert _request(client).status_code == 403
    client.get("/admin/login/")
    response = client.post(
        f"{AUTH}email/request/",
        {},
        format="json",
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
    )
    assert response.status_code == 200
    # The emailed proof itself can be consumed without a browser cookie.
    fresh = APIClient(REMOTE_ADDR="2001:db8::4")
    assert _confirm(fresh, _token()).status_code == 200


@pytest.mark.parametrize("email", [None, "", " "])
def test_optional_registration_email_keeps_absence_as_null(client, email) -> None:
    phone = _phone()
    assert _register(client, phone, email=email).status_code == 201
    assert User.objects.get(phone=phone).email is None


@pytest.mark.parametrize(
    "email",
    ["not-an-email", "a@example.test\r\nBcc: attacker@example.test", "a" * 255 + "@example.test"],
)
def test_registration_rejects_invalid_email(client, email) -> None:
    assert _register(client, _phone(), email=email).status_code == 400
    assert not User.objects.exists()


def test_registration_email_collision_is_generic_and_creates_nothing(client) -> None:
    email = f"collision-{uuid4().hex}@example.test"
    owner = UserFactory(email=email)
    count = UserProfile.objects.count()
    phone = _phone()
    conflict = _register(client, phone, email=email.upper())
    created = _register(client, _phone(), email=f"new-{uuid4().hex}@example.test")
    assert conflict.status_code == created.status_code == 201
    assert conflict.json() == created.json()
    assert not User.objects.filter(phone=phone).exists()
    assert UserProfile.objects.count() == count + 1
    owner.refresh_from_db()
    assert owner.email == email and owner.check_password(FACTORY_PASSWORD)
    assert len(mail.outbox) == 1


def test_reregistering_does_not_change_saved_email_or_verification_state(client) -> None:
    user = _member()
    assert _register(client, user.phone, email="replacement@example.test").status_code == 201
    original = user.email
    user.refresh_from_db()
    assert user.email == original and not user.is_email_verified


def test_request_rechecks_account_under_lock() -> None:
    user = _member()
    User.objects.filter(pk=user.pk).update(account_status=AccountStatus.SUSPENDED)
    with pytest.raises(InvalidCredentials):
        email_verification.request_verification(user=user, source_ip="2001:db8::5")
    assert not VerificationToken.objects.exists()


def test_failed_account_update_does_not_consume_verification_proof(client, monkeypatch) -> None:
    user = _member()
    _authorize(client, user)
    assert _request(client).status_code == 200
    token = _token()
    with monkeypatch.context() as context:

        def fail_save(self, **kwargs):
            raise RuntimeError("Simulated account update failure")

        context.setattr(User, "save", fail_save)
        response = _confirm(client, token)
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"
        assert "Simulated account update failure" not in response.content.decode()
    user.refresh_from_db()
    assert not user.is_email_verified
    assert VerificationToken.objects.get().consumed_at is None
    assert _confirm(client, token).status_code == 200
