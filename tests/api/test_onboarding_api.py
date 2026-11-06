"""Phone onboarding contracts, proof ownership and durable failed-attempt counters."""

from __future__ import annotations

import ipaddress
import re
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.constants import AccountStatus, OTPPurpose, VerificationStatus
from apps.accounts.models import OTPCode, User, UserProfile
from apps.accounts.services import otp
from config import ratelimit
from config.phone_verification import DeliveryUnavailable, LocalEmailCapture
from tests.api.test_authentication_api import _keys
from tests.factories import UnverifiedUserFactory, UserFactory

pytestmark = [pytest.mark.api, pytest.mark.security, pytest.mark.django_db]
AUTH = "/api/v1/auth/"
PASSWORD = "xK7#mQp2!vLz9Rt"  # pragma: allowlist secret


@pytest.fixture
def client() -> APIClient:
    address = ipaddress.IPv6Address(int(ipaddress.IPv6Address("2001:db8::")) + (uuid4().int >> 64))
    return APIClient(REMOTE_ADDR=str(address))


def _phone() -> str:
    return f"+234803{uuid4().int % 10**7:07d}"


def _register(client: APIClient, target_phone: str, **changes: Any):
    return client.post(
        f"{AUTH}register/",
        {"full_name": "Adaeze Okonkwo", "phone": target_phone, "password": PASSWORD, **changes},
        format="json",
    )


def _code() -> str:
    match = re.search(r"\b[0-9]{6}\b", mail.outbox[-1].body)
    assert match is not None
    return match.group()


def _verify(client: APIClient, phone: str, code: str):
    return client.post(f"{AUTH}phone/verify/", {"phone": phone, "code": code}, format="json")


def test_register_verify_and_login(client: APIClient, monkeypatch, caplog) -> None:
    keys = _keys()
    monkeypatch.setattr(settings, "JWT_SIGNING_KEY", keys[0])
    monkeypatch.setattr(settings, "JWT_VERIFYING_KEY", keys[1])
    phone = _phone()
    local = "0" + phone[4:]
    response = _register(client, local)
    assert response.status_code == 201
    assert set(response.json()["data"]) == {"message"}
    user = User.objects.get(phone=phone)
    assert UserProfile.objects.filter(user=user).exists()
    assert not user.is_phone_verified
    assert check_password(PASSWORD, user.password)
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [f"{phone[1:]}@phone.invalid"]
    code = _code()
    assert check_password(code, OTPCode.objects.get().code_hash)
    assert code not in response.content.decode()
    verified = _verify(client, local, code)
    assert verified.status_code == 200
    assert verified.json()["data"] == {
        "id": str(user.public_id),
        "phone": phone,
        "is_phone_verified": True,
        "verification_status": VerificationStatus.PHONE_VERIFIED,
    }
    assert _verify(client, phone, code).status_code == 400
    login = client.post(f"{AUTH}login/", {"identifier": phone, "password": PASSWORD}, format="json")
    assert login.status_code == 200
    assert login.json()["data"]["user"]["id"] == str(user.public_id)
    assert code not in caplog.text
    assert PASSWORD not in caplog.text


def test_registration_does_not_disclose_existing_accounts_or_change_credentials(
    client: APIClient,
) -> None:
    phone = _phone()
    first = _register(client, phone)
    second = _register(client, phone, full_name="Someone else", password="bQ4$nW8*eR2!yT6u")
    user = User.objects.get(phone=phone)
    assert user.full_name == "Adaeze Okonkwo"
    assert user.check_password(PASSWORD)
    User.objects.filter(pk=user.pk).update(is_phone_verified=True)
    count = len(mail.outbox)
    third = _register(client, phone)
    assert first.status_code == second.status_code == third.status_code == 201
    assert first.json() == second.json() == third.json()
    assert len(mail.outbox) == count
    assert User.objects.count() == 1


@pytest.mark.parametrize("kind", ["unknown", "unverified", "verified"])
def test_resend_is_generic_and_only_sends_to_unverified_accounts(
    client: APIClient, kind: str
) -> None:
    phone = _phone()
    if kind != "unknown":
        UserFactory(phone=phone, is_phone_verified=kind == "verified")
    response = client.post(f"{AUTH}otp/request/", {"phone": phone}, format="json")
    assert response.status_code == 200
    assert set(response.json()["data"]) == {"message"}
    assert len(getattr(mail, "outbox", [])) == (1 if kind == "unverified" else 0)
    assert OTPCode.objects.count() == (1 if kind == "unverified" else 0)


def test_resend_replaces_the_previous_code(client: APIClient, monkeypatch) -> None:
    codes = iter(["012345", "654321"])
    monkeypatch.setattr(otp, "_generate_code", lambda: next(codes))
    phone = _phone()
    assert _register(client, phone).status_code == 201
    assert client.post(f"{AUTH}otp/request/", {"phone": phone}, format="json").status_code == 200
    assert _verify(client, phone, "012345").status_code == 400
    assert _verify(client, phone, "654321").status_code == 200


def test_failed_guesses_commit_and_exhaust_the_code(client: APIClient, monkeypatch) -> None:
    monkeypatch.setattr(otp, "_generate_code", lambda: "012345")
    phone = _phone()
    _register(client, phone)
    for attempt in range(1, 4):
        assert _verify(client, phone, "000000").status_code == 400
        record = OTPCode.objects.get()
        assert record.attempts == attempt
    assert record.consumed_at is not None
    assert _verify(client, phone, "012345").status_code == 400
    assert not User.objects.get(phone=phone).is_phone_verified


@pytest.mark.parametrize("reason", ["expired", "consumed", "wrong_purpose", "unknown"])
def test_unusable_codes_have_the_same_public_error(client: APIClient, reason: str) -> None:
    phone = _phone()
    user = UnverifiedUserFactory(phone=phone)
    issued = otp.issue(destination=phone, purpose=OTPPurpose.PHONE_VERIFY, user=user)
    if reason == "expired":
        OTPCode.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    elif reason == "consumed":
        OTPCode.objects.update(consumed_at=timezone.now())
    elif reason == "wrong_purpose":
        OTPCode.objects.update(purpose=OTPPurpose.LOGIN)
    else:
        OTPCode.objects.all().delete()
    response = _verify(client, phone, issued.code)
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "The verification code is invalid or expired."
    user.refresh_from_db()
    assert not user.is_phone_verified


@pytest.mark.parametrize("email_verified", [False, True])
def test_verification_summary_matches_both_channel_flags(
    client: APIClient, email_verified: bool
) -> None:
    user = UnverifiedUserFactory(is_email_verified=email_verified)
    issued = otp.issue(destination=user.phone, purpose=OTPPurpose.PHONE_VERIFY, user=user)
    assert _verify(client, user.phone, issued.code).status_code == 200
    user.refresh_from_db()
    assert user.verification_status == (
        VerificationStatus.FULLY_VERIFIED if email_verified else VerificationStatus.PHONE_VERIFIED
    )


@pytest.mark.parametrize("bound", [False, True])
def test_code_must_belong_to_the_account_it_verifies(client: APIClient, bound: bool) -> None:
    user = UnverifiedUserFactory()
    other = UserFactory() if bound else None
    issued = otp.issue(destination=user.phone, purpose=OTPPurpose.PHONE_VERIFY, user=other)
    assert _verify(client, user.phone, issued.code).status_code == 400
    issued.record.refresh_from_db()
    user.refresh_from_db()
    assert issued.record.consumed_at is None
    assert not user.is_phone_verified


@pytest.mark.parametrize("status", [AccountStatus.SUSPENDED, AccountStatus.CLOSED, "UNKNOWN"])
def test_verification_cannot_reactivate_accounts(client: APIClient, status: str) -> None:
    user = UnverifiedUserFactory(account_status=status)
    issued = otp.issue(destination=user.phone, purpose=OTPPurpose.PHONE_VERIFY, user=user)
    assert _verify(client, user.phone, issued.code).status_code == 400
    user.refresh_from_db()
    assert user.account_status == status
    assert not user.is_phone_verified


@pytest.mark.parametrize("path", ["register/", "phone/verify/", "otp/request/"])
def test_ip_limits_refuse_before_database_queries(
    client: APIClient, path: str, monkeypatch, django_assert_num_queries
) -> None:
    monkeypatch.setattr(
        ratelimit, "consume", lambda scope, identifier: ratelimit.Decision(scope, False, 50, 0, 23)
    )
    with django_assert_num_queries(0):
        response = client.post(
            f"{AUTH}{path}",
            {"full_name": "Adaeze", "phone": _phone(), "password": PASSWORD, "code": "012345"},
            format="json",
        )
    assert response.status_code == 429
    assert response["Retry-After"] == "23"


@pytest.mark.parametrize("verified", [False, True])
def test_verified_and_unverified_registration_share_destination_limits(
    client: APIClient, monkeypatch, verified: bool
) -> None:
    phone = _phone()
    UserFactory(phone=phone, is_phone_verified=verified)
    limits = {**settings.RATE_LIMITS, "otp_issue": {"limit": 1, "window_seconds": 900}}
    monkeypatch.setattr(settings, "RATE_LIMITS", limits)
    assert _register(client, phone).status_code == 201
    response = _register(client, phone)
    assert response.status_code == 429
    assert int(response["Retry-After"]) > 0


@pytest.mark.parametrize(
    "body",
    [
        {"full_name": ""},
        {"full_name": " "},
        {"full_name": "x" * 151},
        {"phone": "not-a-phone"},
        {"password": "password"},
        {"password": "x" * 4097},
    ],
)
def test_invalid_registration_writes_nothing(client: APIClient, body: dict[str, Any]) -> None:
    assert _register(client, _phone(), **body).status_code == 400
    assert User.objects.count() == UserProfile.objects.count() == OTPCode.objects.count() == 0


@pytest.mark.parametrize(
    "code", ["12345", "1234567", "abcdef", "\uff11\uff12\uff13\uff14\uff15\uff16", "123456\n", None]
)
def test_verification_requires_exactly_six_ascii_digits(client: APIClient, code: Any) -> None:
    assert _verify(client, _phone(), code).status_code == 400


@pytest.mark.parametrize("purpose", ["LOGIN", "PASSWORD_RESET", "TRANSACTION"])
def test_public_resend_does_not_issue_codes_for_other_purposes(
    client: APIClient, purpose: str
) -> None:
    assert (
        client.post(
            f"{AUTH}otp/request/", {"phone": _phone(), "purpose": purpose}, format="json"
        ).status_code
        == 400
    )
    assert not OTPCode.objects.exists()


def test_unconfigured_delivery_refuses_before_creating_an_account(
    client: APIClient, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "PHONE_VERIFICATION_BACKEND", "")
    assert _register(client, _phone()).status_code == 503
    assert not User.objects.exists()


def test_delivery_failure_keeps_the_account_recoverable(client: APIClient, monkeypatch) -> None:
    phone = _phone()

    def unavailable(self, **kwargs):
        raise OSError("Synthetic delivery failure")

    with monkeypatch.context() as patch:
        patch.setattr(LocalEmailCapture, "send", unavailable)
        assert _register(client, phone).status_code == 503
    assert User.objects.filter(phone=phone).exists()
    assert OTPCode.objects.get().consumed_at is not None
    assert client.post(f"{AUTH}otp/request/", {"phone": phone}, format="json").status_code == 200
    assert _verify(client, phone, _code()).status_code == 200


def test_verification_and_code_consumption_rollback_together(
    client: APIClient, monkeypatch
) -> None:
    user = UnverifiedUserFactory()
    issued = otp.issue(destination=user.phone, purpose=OTPPurpose.PHONE_VERIFY, user=user)

    def fail_save(self, **kwargs):
        raise RuntimeError("Synthetic account update failure")

    with monkeypatch.context() as patch:
        patch.setattr(User, "save", fail_save)
        assert _verify(client, user.phone, issued.code).status_code == 500
    issued.record.refresh_from_db()
    user.refresh_from_db()
    assert issued.record.consumed_at is None
    assert not user.is_phone_verified


def test_local_capture_cannot_send_via_a_production_mail_server(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DEBUG", False)
    monkeypatch.setattr(settings, "EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
    monkeypatch.setattr(settings, "EMAIL_HOST", "smtp.example.test")
    with pytest.raises(DeliveryUnavailable):
        LocalEmailCapture().check_available()
