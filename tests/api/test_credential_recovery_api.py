"""Credential recovery, proof requirements, revocation and secret-free auditing."""

from __future__ import annotations

import ipaddress
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from django.conf import settings
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.constants import AccountStatus, OTPPurpose, TokenPurpose
from apps.accounts.models import OTPCode, User, UserSession, VerificationToken
from apps.accounts.services import otp, recovery
from apps.audit.models import AuditLog
from config import ratelimit
from config.phone_verification import LocalEmailCapture
from tests.api.test_authentication_api import _bearer, _keys, _login
from tests.factories import UserFactory
from tests.factories.accounts import FACTORY_PASSWORD

pytestmark = [pytest.mark.api, pytest.mark.security, pytest.mark.django_db]
AUTH = "/api/v1/auth/"
NEW_PASSWORD = "rT9!mP4@vL7#xQ2"  # pragma: allowlist secret


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[str, str]:
    return _keys()


@pytest.fixture(autouse=True)
def _configure_keys(monkeypatch, rsa_keys: tuple[str, str]) -> None:
    monkeypatch.setattr(settings, "JWT_SIGNING_KEY", rsa_keys[0])
    monkeypatch.setattr(settings, "JWT_VERIFYING_KEY", rsa_keys[1])


@pytest.fixture
def client() -> APIClient:
    address = ipaddress.IPv6Address(int(ipaddress.IPv6Address("2001:db8::")) + (uuid4().int >> 64))
    return APIClient(REMOTE_ADDR=str(address))


def _member(**overrides: Any) -> User:
    return UserFactory(
        **{
            "phone": f"+234803{uuid4().int % 10**7:07d}",
            "email": f"recovery-{uuid4().hex}@example.test",
            **overrides,
        }
    )


def _secret() -> str:
    return mail.outbox[-1].body.split(" is ", 1)[1].split(". It expires", 1)[0]


def _request(client: APIClient, identifier: str):
    return client.post(f"{AUTH}password/reset/", {"identifier": identifier}, format="json")


def _confirm(client: APIClient, **proof: Any):
    return client.post(
        f"{AUTH}password/reset/confirm/", {"new_password": NEW_PASSWORD, **proof}, format="json"
    )


def _authorize(client: APIClient, user: User) -> dict[str, Any]:
    pair = _login(client, user)
    _bearer(client, pair["access_token"])
    return pair


@pytest.mark.parametrize("channel", ["phone", "email"])
def test_recovery_changes_password_and_revokes_every_login(
    client: APIClient, channel: str, caplog
) -> None:
    user = _member()
    first = _authorize(client, user)
    other = _login(client, user)
    response = _request(client, user.phone if channel == "phone" else str(user.email).upper())
    assert response.status_code == 200
    assert set(response.json()["data"]) == {"message"}
    secret = _secret()
    if channel == "phone":
        record = OTPCode.objects.get(purpose=OTPPurpose.PASSWORD_RESET)
        proof = {"phone": user.phone, "code": secret}
        assert len(secret) == 6
        assert record.code_hash != secret
    else:
        record = VerificationToken.objects.get(purpose=TokenPurpose.PASSWORD_RESET)
        proof = {"reset_token": secret}
        assert len(secret) == 101
        assert record.token_hash != secret
        assert mail.outbox[-1].to == [user.email]
    assert secret not in response.content.decode()
    confirmed = _confirm(client, **proof)
    assert confirmed.status_code == 204
    assert not confirmed.content
    user.refresh_from_db()
    record.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)
    assert not user.check_password(FACTORY_PASSWORD)
    assert record.consumed_at is not None
    assert not UserSession.objects.filter(user=user, revoked_at__isnull=True).exists()
    for pair in (first, other):
        _bearer(client, pair["access_token"])
        assert client.get(f"{AUTH}sessions/").status_code == 401
        assert (
            client.post(
                f"{AUTH}token/refresh/", {"refresh_token": pair["refresh_token"]}, format="json"
            ).status_code
            == 401
        )
    assert _confirm(client, **proof).status_code == 400
    assert (
        client.post(
            f"{AUTH}login/", {"identifier": user.phone, "password": NEW_PASSWORD}, format="json"
        ).status_code
        == 200
    )
    for hidden in (NEW_PASSWORD, FACTORY_PASSWORD, secret):
        assert hidden not in caplog.text


@pytest.mark.parametrize(
    "kind", ["unknown", "unverified_phone", "unverified_email", "suspended", "closed"]
)
def test_recovery_requests_do_not_disclose_or_modify_unavailable_accounts(
    client: APIClient, kind: str
) -> None:
    user = _member(
        is_phone_verified=kind != "unverified_phone",
        is_email_verified=kind != "unverified_email",
        account_status=AccountStatus.SUSPENDED
        if kind == "suspended"
        else AccountStatus.CLOSED
        if kind == "closed"
        else AccountStatus.ACTIVE,
    )
    identifier = str(user.email) if kind == "unverified_email" else user.phone
    if kind == "unknown":
        identifier = _member().phone
        User.objects.filter(phone=identifier).delete()
    response = _request(client, identifier)
    assert response.status_code == 200
    assert response.json() == {
        "data": {
            "message": "If a verified recovery channel is available, reset instructions will be sent."
        }
    }
    assert not getattr(mail, "outbox", [])
    assert not OTPCode.objects.exists()
    assert not VerificationToken.objects.exists()
    user.refresh_from_db()
    assert user.check_password(FACTORY_PASSWORD)


def test_phone_and_email_reset_requests_share_the_account_budget(client: APIClient) -> None:
    user = _member()
    for identifier in (user.phone, str(user.email), str(user.email).upper()):
        assert _request(client, identifier).status_code == 200
    response = _request(client, user.phone)
    assert response.status_code == 429
    assert int(response["Retry-After"]) > 0


def test_switching_recovery_channel_invalidates_the_previous_proof(client: APIClient) -> None:
    user = _member()
    _request(client, str(user.email))
    first = _secret()
    _request(client, user.phone)
    current = _secret()
    assert _confirm(client, reset_token=first).status_code == 400
    assert _confirm(client, phone=user.phone, code=current).status_code == 204


@pytest.mark.parametrize("channel", ["phone", "email"])
def test_weak_replacement_password_keeps_the_proof_usable(client: APIClient, channel: str) -> None:
    user = _member()
    _request(client, user.phone if channel == "phone" else str(user.email))
    secret = _secret()
    proof = {"phone": user.phone, "code": secret} if channel == "phone" else {"reset_token": secret}
    assert _confirm(client, new_password="password", **proof).status_code == 400
    user.refresh_from_db()
    assert user.check_password(FACTORY_PASSWORD)
    record = OTPCode.objects.get() if channel == "phone" else VerificationToken.objects.get()
    assert record.consumed_at is None
    assert _confirm(client, **proof).status_code == 204


def test_wrong_reset_codes_are_counted_and_exhausted(client: APIClient, monkeypatch) -> None:
    monkeypatch.setattr(otp, "_generate_code", lambda: "012345")
    user = _member()
    _request(client, user.phone)
    for count in range(1, 4):
        assert _confirm(client, phone=user.phone, code="000000").status_code == 400
        assert OTPCode.objects.get().attempts == count
    assert OTPCode.objects.get().consumed_at is not None
    assert _confirm(client, phone=user.phone, code="012345").status_code == 400
    user.refresh_from_db()
    assert user.check_password(FACTORY_PASSWORD)


@pytest.mark.parametrize(
    "reason", ["expired", "wrong_purpose", "wrong_owner", "email_changed", "inactive"]
)
def test_email_reset_proof_is_bound_to_its_account_and_state(
    client: APIClient, reason: str
) -> None:
    user = _member()
    _request(client, str(user.email))
    secret = _secret()
    if reason == "expired":
        stamp = timezone.now()
        VerificationToken.objects.update(
            created_at=stamp - timedelta(minutes=20), expires_at=stamp - timedelta(seconds=1)
        )
    elif reason == "wrong_purpose":
        VerificationToken.objects.update(purpose=TokenPurpose.EMAIL_VERIFY)
    elif reason == "wrong_owner":
        secret = str(_member().public_id) + "." + secret.split(".", 1)[1]
    elif reason == "email_changed":
        User.objects.filter(pk=user.pk).update(email="changed@example.test")
    else:
        User.objects.filter(pk=user.pk).update(account_status=AccountStatus.SUSPENDED)
    assert _confirm(client, reset_token=secret).status_code == 400
    user.refresh_from_db()
    assert user.check_password(FACTORY_PASSWORD)


def test_phone_verification_code_cannot_reset_a_password(client: APIClient) -> None:
    user = _member()
    issued = otp.issue(destination=user.phone, purpose=OTPPurpose.PHONE_VERIFY, user=user)
    assert _confirm(client, phone=user.phone, code=issued.code).status_code == 400
    issued.record.refresh_from_db()
    assert issued.record.consumed_at is None


def test_password_recovery_clears_a_credential_lockout(client: APIClient) -> None:
    user = _member(failed_login_attempts=5, locked_until=timezone.now() + timedelta(minutes=5))
    assert _request(client, user.phone).status_code == 200
    assert _confirm(client, phone=user.phone, code=_secret()).status_code == 204
    user.refresh_from_db()
    assert user.failed_login_attempts == 0
    assert user.locked_until is None


@pytest.mark.parametrize("path", ["password/reset/", "password/reset/confirm/"])
def test_public_recovery_ip_limits_precede_database_queries(
    client: APIClient, path: str, monkeypatch, django_assert_num_queries
) -> None:
    monkeypatch.setattr(
        ratelimit, "consume", lambda scope, identifier: ratelimit.Decision(scope, False, 50, 0, 17)
    )
    with django_assert_num_queries(0):
        response = client.post(
            f"{AUTH}{path}",
            {
                "identifier": "unknown@example.test",
                "reset_token": "fabricated",
                "new_password": NEW_PASSWORD,
            },
            format="json",
        )
    assert response.status_code == 429
    assert response["Retry-After"] == "17"


@pytest.mark.parametrize(
    "proof",
    [
        {},
        {"phone": "+2348031234567"},
        {"code": "012345"},
        {"reset_token": "fake", "phone": "+2348031234567", "code": "012345"},
    ],
)
def test_reset_confirmation_requires_one_complete_proof(
    client: APIClient, proof: dict[str, Any]
) -> None:
    assert _confirm(client, **proof).status_code == 400


def test_delivery_failure_leaves_credentials_unchanged_and_resend_works(
    client: APIClient, monkeypatch
) -> None:
    user = _member()

    def fail(self, **kwargs):
        raise OSError("Synthetic recovery delivery failure")

    with monkeypatch.context() as patch:
        patch.setattr(LocalEmailCapture, "send", fail)
        assert _request(client, str(user.email)).status_code == 503
    assert VerificationToken.objects.get().consumed_at is not None
    user.refresh_from_db()
    assert user.check_password(FACTORY_PASSWORD)
    assert _request(client, str(user.email)).status_code == 200
    assert _confirm(client, reset_token=_secret()).status_code == 204


def test_unconfigured_delivery_does_not_create_a_reset_proof(
    client: APIClient, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "PHONE_VERIFICATION_BACKEND", "")
    assert _request(client, _member().phone).status_code == 503
    assert not OTPCode.objects.exists()
    assert not VerificationToken.objects.exists()


def test_password_change_requires_the_old_password_and_revokes_tokens(client: APIClient) -> None:
    user = _member()
    pair = _authorize(client, user)
    assert (
        client.post(
            f"{AUTH}password/change/",
            {"current_password": "wrong", "new_password": NEW_PASSWORD},
            format="json",
        ).status_code
        == 401
    )
    user.refresh_from_db()
    assert user.failed_login_attempts == 1
    assert user.check_password(FACTORY_PASSWORD)
    assert (
        client.post(
            f"{AUTH}password/change/",
            {"current_password": FACTORY_PASSWORD, "new_password": NEW_PASSWORD},
            format="json",
        ).status_code
        == 204
    )
    assert client.get(f"{AUTH}sessions/").status_code == 401
    assert (
        client.post(
            f"{AUTH}token/refresh/", {"refresh_token": pair["refresh_token"]}, format="json"
        ).status_code
        == 401
    )
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)
    assert user.failed_login_attempts == 0


def test_password_change_invalidates_an_outstanding_reset_link(client: APIClient) -> None:
    user = _member()
    _request(client, str(user.email))
    secret = _secret()
    _authorize(client, user)
    assert (
        client.post(
            f"{AUTH}password/change/",
            {"current_password": FACTORY_PASSWORD, "new_password": NEW_PASSWORD},
            format="json",
        ).status_code
        == 204
    )
    assert _confirm(client, reset_token=secret).status_code == 400


@pytest.mark.parametrize("path", ["password/change/", "pin/set/", "pin/change/"])
def test_credential_mutations_require_authentication(client: APIClient, path: str) -> None:
    assert client.post(f"{AUTH}{path}", {}, format="json").status_code == 401


def test_pin_setup_requires_password_and_cannot_replace_an_existing_pin(
    client: APIClient, caplog
) -> None:
    user = _member()
    pair = _authorize(client, user)
    assert (
        client.post(
            f"{AUTH}pin/set/", {"current_password": "wrong", "pin": "0123"}, format="json"
        ).status_code
        == 401
    )
    user.refresh_from_db()
    assert user.pin_hash is None
    assert (
        client.post(
            f"{AUTH}pin/set/", {"current_password": FACTORY_PASSWORD, "pin": "0123"}, format="json"
        ).status_code
        == 204
    )
    user.refresh_from_db()
    assert user.check_pin("0123")
    assert user.pin_hash != "0123"
    assert client.get(f"{AUTH}sessions/").status_code == 401
    assert (
        client.post(
            f"{AUTH}token/refresh/", {"refresh_token": pair["refresh_token"]}, format="json"
        ).status_code
        == 401
    )
    _authorize(client, user)
    response = client.post(
        f"{AUTH}pin/set/", {"current_password": FACTORY_PASSWORD, "pin": "4567"}, format="json"
    )
    assert response.status_code == 400
    assert "Use PIN change" in response.json()["error"]["message"]
    user.refresh_from_db()
    assert user.check_pin("0123")
    assert AuditLog.objects.get().action == "user.pin_set"
    assert AuditLog.objects.get().previous_value == {"pin_set": False}
    assert AuditLog.objects.get().new_value == {"pin_set": True}
    assert user.pin_hash not in caplog.text
    assert all("0123" not in record.getMessage() for record in caplog.records)


def test_pin_change_requires_the_current_pin_and_revokes_every_session(client: APIClient) -> None:
    user = _member()
    user.set_pin("4821")
    user.save(update_fields=["pin_hash"])
    _authorize(client, user)
    assert (
        client.post(
            f"{AUTH}pin/change/", {"current_pin": "0000", "new_pin": "0123"}, format="json"
        ).status_code
        == 400
    )
    user.refresh_from_db()
    assert user.failed_login_attempts == 1
    assert user.check_pin("4821")
    assert (
        client.post(
            f"{AUTH}pin/change/", {"current_pin": "4821", "new_pin": "0123"}, format="json"
        ).status_code
        == 204
    )
    user.refresh_from_db()
    assert user.check_pin("0123")
    assert not user.check_pin("4821")
    assert user.failed_login_attempts == 0
    assert client.get(f"{AUTH}sessions/").status_code == 401
    audit = AuditLog.objects.get()
    assert audit.action == "user.pin_changed"
    assert audit.previous_value == audit.new_value == {"pin_set": True}


def test_pin_guesses_are_limited_and_lockout_is_durable(client: APIClient) -> None:
    user = _member()
    user.set_pin("4821")
    user.save(update_fields=["pin_hash"])
    _authorize(client, user)
    for attempt in range(1, 6):
        assert (
            client.post(
                f"{AUTH}pin/change/", {"current_pin": "0000", "new_pin": "0123"}, format="json"
            ).status_code
            == 400
        )
        user.refresh_from_db()
        assert user.failed_login_attempts == attempt
    assert user.is_locked
    assert (
        client.post(
            f"{AUTH}pin/change/", {"current_pin": "4821", "new_pin": "0123"}, format="json"
        ).status_code
        == 429
    )
    assert not AuditLog.objects.exists()


@pytest.mark.parametrize("path", ["password/change/", "pin/set/", "pin/change/"])
def test_cookie_credential_changes_require_csrf(path: str) -> None:
    client = APIClient(enforce_csrf_checks=True)
    user = _member()
    client.force_login(user)
    assert (
        client.post(
            f"{AUTH}{path}",
            {
                "current_password": FACTORY_PASSWORD,
                "new_password": NEW_PASSWORD,
                "pin": "0123",
                "current_pin": "4821",
                "new_pin": "0123",
            },
            format="json",
        ).status_code
        == 403
    )
    user.refresh_from_db()
    assert user.check_password(FACTORY_PASSWORD)
    assert user.pin_hash is None


@pytest.mark.parametrize("path", ["password/change/", "pin/set/", "pin/change/"])
def test_all_browser_sessions_expire_after_a_credential_change(
    client: APIClient, path: str
) -> None:
    user = _member()
    if path == "pin/change/":
        user.set_pin("4821")
        user.save(update_fields=["pin_hash"])
    browsers = [APIClient(), APIClient()]
    for browser in browsers:
        browser.force_login(user)
        assert browser.get(f"{AUTH}sessions/").status_code == 200
    _authorize(client, user)
    assert (
        client.post(
            f"{AUTH}{path}",
            {
                "current_password": FACTORY_PASSWORD,
                "new_password": NEW_PASSWORD,
                "pin": "0123",
                "current_pin": "4821",
                "new_pin": "0123",
            },
            format="json",
        ).status_code
        == 204
    )
    for browser in browsers:
        assert browser.get(f"{AUTH}sessions/").status_code == 401


@pytest.mark.parametrize(
    "value", ["123", "12345", "abcd", "1234\n", "\uff11\uff12\uff13\uff14", 1234]
)
def test_pins_require_four_ascii_digits(client: APIClient, value: Any) -> None:
    user = _member()
    _authorize(client, user)
    assert (
        client.post(
            f"{AUTH}pin/set/", {"current_password": FACTORY_PASSWORD, "pin": value}, format="json"
        ).status_code
        == 400
    )
    user.refresh_from_db()
    assert user.pin_hash is None


@pytest.mark.parametrize("path", ["password/change/", "pin/change/"])
def test_replacement_credential_must_differ_from_the_current_one(
    client: APIClient, path: str
) -> None:
    user = _member()
    user.set_pin("4821")
    user.save(update_fields=["pin_hash"])
    _authorize(client, user)
    response = client.post(
        f"{AUTH}{path}",
        {
            "current_password": FACTORY_PASSWORD,
            "new_password": FACTORY_PASSWORD,
            "current_pin": "4821",
            "new_pin": "4821",
        },
        format="json",
    )
    assert response.status_code == 400
    assert "different" in response.json()["error"]["message"]
    assert UserSession.objects.filter(user=user, revoked_at__isnull=True).exists()


def test_pin_change_rolls_back_if_the_audit_write_fails(client: APIClient, monkeypatch) -> None:
    user = _member()
    _authorize(client, user)

    def fail(**kwargs):
        raise RuntimeError("Synthetic audit storage failure")

    monkeypatch.setattr(recovery, "record_audit", fail)
    assert (
        client.post(
            f"{AUTH}pin/set/", {"current_password": FACTORY_PASSWORD, "pin": "0123"}, format="json"
        ).status_code
        == 500
    )
    user.refresh_from_db()
    assert user.pin_hash is None
    assert UserSession.objects.filter(user=user, revoked_at__isnull=True).exists()


@pytest.mark.parametrize("path", ["password/change/", "pin/set/", "pin/change/"])
def test_cookie_credential_change_succeeds_with_csrf_and_logs_out(path: str) -> None:
    user = _member()
    if path == "pin/change/":
        user.set_pin("4821")
        user.save(update_fields=["pin_hash"])
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(user)
    client.get("/admin/login/")
    csrf = client.cookies["csrftoken"].value
    response = client.post(
        f"{AUTH}{path}",
        {
            "current_password": FACTORY_PASSWORD,
            "new_password": NEW_PASSWORD,
            "pin": "0123",
            "current_pin": "4821",
            "new_pin": "0123",
        },
        format="json",
        HTTP_X_CSRFTOKEN=csrf,
    )
    assert response.status_code == 204
    assert client.get(f"{AUTH}sessions/").status_code == 401


def test_phone_reset_code_must_belong_to_the_account(client: APIClient) -> None:
    user = _member()
    issued = otp.issue(destination=user.phone, purpose=OTPPurpose.PASSWORD_RESET, user=_member())
    assert _confirm(client, phone=user.phone, code=issued.code).status_code == 400
    issued.record.refresh_from_db()
    user.refresh_from_db()
    assert issued.record.consumed_at is None
    assert user.check_password(FACTORY_PASSWORD)


def test_password_reset_and_session_revocation_rollback_together(
    client: APIClient, monkeypatch
) -> None:
    user = _member()
    _authorize(client, user)
    _request(client, user.phone)
    secret = _secret()

    def fail(user):
        raise RuntimeError("Synthetic session revocation failure")

    with monkeypatch.context() as patch:
        patch.setattr(recovery.sessions, "revoke_all_for_user", fail)
        assert _confirm(client, phone=user.phone, code=secret).status_code == 500
    user.refresh_from_db()
    assert user.check_password(FACTORY_PASSWORD)
    assert OTPCode.objects.get().consumed_at is None
    assert UserSession.objects.filter(user=user, revoked_at__isnull=True).exists()
    assert _confirm(client, phone=user.phone, code=secret).status_code == 204
