"""Complete second-factor journeys and the boundaries around password proofs."""

from __future__ import annotations

import base64
import io
import uuid
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core import mail
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.constants import AccountStatus
from apps.accounts.models import MFAChallenge, MFAEnrollment, MFARecoveryCode, User, UserSession
from apps.accounts.services import mfa
from config import ratelimit
from config.phone_verification import DeliveryUnavailable
from tests.api.test_authentication_api import AUTH, _bearer, _claims, _keys, _login
from tests.factories import UserFactory
from tests.factories.accounts import FACTORY_PASSWORD

pytestmark = [pytest.mark.api, pytest.mark.security, pytest.mark.django_db]


@pytest.fixture(scope="module")
def keypair() -> tuple[str, str]:
    return _keys()


@pytest.fixture(autouse=True)
def security_settings(monkeypatch, keypair) -> None:
    monkeypatch.setattr(settings, "JWT_SIGNING_KEY", keypair[0])
    monkeypatch.setattr(settings, "JWT_VERIFYING_KEY", keypair[1])
    monkeypatch.setattr(settings, "MFA_ENCRYPTION_KEYS", Fernet.generate_key().decode())


@pytest.fixture
def client() -> APIClient:
    return APIClient(REMOTE_ADDR=f"2001:db8::{uuid.uuid4().int % 65535:x}")


def _post(client, route, **body):
    return client.post(f"{AUTH}{route}/", body, format="json")


def _code(seed: str, offset: int = 0) -> str:
    return mfa.totp(seed, int(timezone.now().timestamp()) // 30 + offset)


def _begin(client, user, method="TOTP", token=None):
    body = {"method": method, "current_password": FACTORY_PASSWORD}
    if token:
        body["mfa_token"] = token
    response = _post(client, "mfa/enable", **body)
    assert response.status_code == 200, response.json()
    return response.json()["data"]


def _enroll(client, user, method="TOTP", token=None):
    if token is None:
        _bearer(client, _login(client, user)["access_token"])
    setup = _begin(client, user, method, token)
    code = _code(setup["secret"], -1) if method == "TOTP" else _sms_code()
    body = {"code": code}
    if token:
        body["mfa_token"] = token
    response = _post(client, "mfa/confirm", **body)
    assert response.status_code == 200, response.json()
    assert response["Cache-Control"] == "no-store"
    client.credentials()
    return setup.get("secret"), response.json()["data"]["recovery_codes"]


def _sms_code() -> str:
    return mail.outbox[-1].body.split(" is ", 1)[1].split(". It expires", 1)[0]


def _challenge(client, user):
    client.credentials()
    result = _login(client, user)
    assert result["mfa_required"] is True
    assert "access_token" not in result and "refresh_token" not in result
    return result["mfa_token"]


def _complete(client, token, code):
    return _post(client, "mfa/challenge", mfa_token=token, code=code)


def test_totp_enrollment_and_login_preserve_mfa_through_rotation(client) -> None:
    user = UserFactory()
    old_pair = _login(client, user)
    _bearer(client, old_pair["access_token"])
    setup = _begin(client, user)
    user.refresh_from_db()
    pending = MFAEnrollment.objects.get(user=user)
    assert not user.two_factor_enabled and user.totp_secret == ""
    assert setup["secret"] not in pending.secret
    assert mfa.decrypt(pending.secret) == setup["secret"]
    assert setup["provisioning_uri"].startswith("otpauth://totp/")
    assert client.get(f"{AUTH}sessions/").status_code == 200
    response = _post(client, "mfa/confirm", code=_code(setup["secret"], -1))
    assert response.status_code == 200
    codes = response.json()["data"]["recovery_codes"]
    assert len(codes) == len(set(codes)) == 10
    assert all(len(code) == 20 for code in codes)
    assert all(code not in str(list(MFARecoveryCode.objects.values())) for code in codes)
    assert not MFAEnrollment.objects.exists()
    user.refresh_from_db()
    assert user.two_factor_enabled and user.two_factor_method == "TOTP" and user.mfa_version == 1
    assert client.get(f"{AUTH}sessions/").status_code == 401
    assert (
        _post(client, "token/refresh", refresh_token=old_pair["refresh_token"]).status_code == 401
    )
    token = _challenge(client, user)
    assert token not in MFAChallenge.objects.get(consumed_at__isnull=True).token_hash
    assert not UserSession.objects.filter(revoked_at__isnull=True).exists()
    pair = _complete(client, token, _code(setup["secret"])).json()["data"]
    session = UserSession.objects.get(public_id=_claims(pair["access_token"])["sid"])
    assert session.mfa_verified and session.mfa_version == user.mfa_version
    _bearer(client, pair["access_token"])
    assert client.get(f"{AUTH}sessions/").status_code == 200
    rotated = _post(client, "token/refresh", refresh_token=pair["refresh_token"])
    assert rotated.status_code == 200
    successor = UserSession.objects.get(
        public_id=_claims(rotated.json()["data"]["access_token"])["sid"]
    )
    assert successor.mfa_verified and successor.mfa_version == session.mfa_version
    assert client.get(f"{AUTH}sessions/").status_code == 200
    assert _post(client, "token/refresh", refresh_token=pair["refresh_token"]).status_code == 401
    assert client.get(f"{AUTH}sessions/").status_code == 401


@pytest.mark.parametrize("flag", ["is_staff", "is_superuser"])
def test_staff_bootstrap_is_restricted_and_mfa_cannot_be_disabled(client, flag) -> None:
    user = UserFactory(**{flag: True})
    token = _challenge(client, user)
    _bearer(client, token)
    assert client.get(f"{AUTH}sessions/").status_code == 401
    client.credentials()
    assert _complete(client, token, "000000").status_code == 401
    seed, codes = _enroll(client, user, token=token)
    assert _complete(client, token, codes[0]).status_code == 401
    fresh = _challenge(client, user)
    pair = _complete(client, fresh, _code(seed)).json()["data"]
    _bearer(client, pair["access_token"])
    assert client.get(f"{AUTH}sessions/").status_code == 200
    response = _post(client, "mfa/disable", current_password=FACTORY_PASSWORD, code=codes[0])
    assert response.status_code == 400
    user.refresh_from_db()
    assert user.two_factor_enabled


def test_sms_setup_confirmation_login_and_recovery(client) -> None:
    user = UserFactory(is_phone_verified=True, phone=f"+234803{uuid.uuid4().int % 10**7:07d}")
    _, codes = _enroll(client, user, "SMS")
    assert "MFA" in mail.outbox[-1].subject
    token = _challenge(client, user)
    code = _sms_code()
    assert code not in MFAChallenge.objects.get(consumed_at__isnull=True).code_hash
    assert _complete(client, token, code).status_code == 200
    assert _complete(client, token, code).status_code == 401
    assert _complete(client, _challenge(client, user), codes[0]).status_code == 200
    assert MFARecoveryCode.objects.get(code_hash=mfa.digest(codes[0], "recovery")).consumed_at


def test_sms_requires_verified_phone_and_no_email_method_is_accepted(client) -> None:
    user = UserFactory(is_phone_verified=False)
    _bearer(client, _login(client, user)["access_token"])
    assert (
        _post(client, "mfa/enable", method="SMS", current_password=FACTORY_PASSWORD).status_code
        == 400
    )
    assert (
        _post(client, "mfa/enable", method="EMAIL", current_password=FACTORY_PASSWORD).status_code
        == 400
    )
    assert not MFAEnrollment.objects.exists()


@pytest.mark.parametrize("key", ["", "invalid-key"])
def test_missing_or_invalid_encryption_key_fails_without_pending_setup(
    client, monkeypatch, key
) -> None:
    user = UserFactory()
    _bearer(client, _login(client, user)["access_token"])
    monkeypatch.setattr(settings, "MFA_ENCRYPTION_KEYS", key)
    assert (
        _post(client, "mfa/enable", method="TOTP", current_password=FACTORY_PASSWORD).status_code
        == 503
    )
    assert not MFAEnrollment.objects.exists()


def test_pending_enrollment_expires_and_wrong_guesses_commit(client) -> None:
    user = UserFactory()
    _bearer(client, _login(client, user)["access_token"])
    setup = _begin(client, user)
    wrong = next(
        f"{n:06d}"
        for n in range(10)
        if f"{n:06d}" not in [_code(setup["secret"], off) for off in (-1, 0, 1)]
    )
    for count in range(1, 4):
        assert _post(client, "mfa/confirm", code=wrong).status_code == 400
        assert MFAEnrollment.objects.get(user=user).attempts == count
    assert _post(client, "mfa/confirm", code=_code(setup["secret"])).status_code == 400
    _begin(client, user)
    MFAEnrollment.objects.filter(user=user).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert _post(client, "mfa/confirm", code="000000").status_code == 400
    user.refresh_from_db()
    assert not user.two_factor_enabled


def test_pending_setup_is_replaced_and_requires_password(client) -> None:
    user = UserFactory()
    _bearer(client, _login(client, user)["access_token"])
    assert (
        _post(client, "mfa/enable", method="TOTP", current_password="incorrect").status_code == 401
    )
    first = _begin(client, user)
    second = _begin(client, user)
    assert first["secret"] != second["secret"]
    assert mfa.decrypt(MFAEnrollment.objects.get(user=user).secret) == second["secret"]


@pytest.mark.parametrize(
    "route,body",
    [
        ("mfa/enable", {"method": "TOTP", "current_password": FACTORY_PASSWORD}),
        ("mfa/confirm", {"code": "000000"}),
        ("mfa/disable", {"current_password": FACTORY_PASSWORD, "code": "000000"}),
    ],
)
def test_setup_without_authentication_or_restricted_proof_is_denied(client, route, body) -> None:
    assert _post(client, route, **body).status_code == 401


def test_restricted_proof_cannot_enroll_another_authenticated_account(client) -> None:
    user = UserFactory(is_staff=True)
    token = _challenge(client, user)
    _bearer(client, _login(client, UserFactory())["access_token"])
    response = _post(
        client, "mfa/enable", method="TOTP", current_password=FACTORY_PASSWORD, mfa_token=token
    )
    assert response.status_code == 401
    assert not MFAEnrollment.objects.exists()


@pytest.mark.parametrize(
    "change", ["expiry", "password", "pin", "status", "device", "version", "role", "phone"]
)
def test_challenge_dies_when_bound_state_changes(client, change) -> None:
    user = UserFactory()
    _, codes = _enroll(client, user)
    token = _challenge(client, user)
    row = MFAChallenge.objects.get(consumed_at__isnull=True)
    if change == "expiry":
        row.expires_at = timezone.now() - timedelta(seconds=1)
        row.save(update_fields=["expires_at"])
    elif change == "device":
        row.device.is_revoked = True
        row.device.save(update_fields=["is_revoked"])
    else:
        updates = {
            "password": {"password": "!changed"},
            "pin": {"pin_hash": "!changed"},
            "status": {"account_status": AccountStatus.SUSPENDED},
            "version": {"mfa_version": 99},
            "role": {"is_staff": True},
            "phone": {"is_phone_verified": False},
        }
        User.objects.filter(pk=user.pk).update(**updates[change])
    assert _complete(client, token, codes[0]).status_code == 401
    assert not UserSession.objects.filter(revoked_at__isnull=True).exists()
    assert not MFARecoveryCode.objects.filter(consumed_at__isnull=False).exists()


def test_challenge_guess_cap_is_not_reset_by_a_success_attempt(client) -> None:
    user = UserFactory()
    seed, codes = _enroll(client, user)
    token = _challenge(client, user)
    wrong = next(
        f"{n:06d}" for n in range(10) if f"{n:06d}" not in [_code(seed, off) for off in (-1, 0, 1)]
    )
    for count in range(1, 4):
        assert _complete(client, token, wrong).status_code == 400
        assert (
            MFAChallenge.objects.get(consumed_at__isnull=True if count < 3 else False).attempts
            == count
        )
    assert _complete(client, token, codes[0]).status_code == 401


def test_new_password_login_supersedes_old_challenge_and_recovery_is_single_use(client) -> None:
    user = UserFactory()
    _, codes = _enroll(client, user)
    first = _challenge(client, user)
    second = _challenge(client, user)
    assert _complete(client, first, codes[0]).status_code == 401
    assert _complete(client, second, codes[0]).status_code == 200
    assert _complete(client, second, codes[1]).status_code == 401
    assert _complete(client, _challenge(client, user), codes[0]).status_code == 400


def test_totp_codes_cannot_be_replayed_across_login_challenges(client) -> None:
    user = UserFactory()
    seed, _ = _enroll(client, user)
    code = _code(seed)
    assert _complete(client, _challenge(client, user), code).status_code == 200
    assert _complete(client, _challenge(client, user), code).status_code == 400


def test_signing_failure_rolls_back_challenge_and_recovery_consumption(client, monkeypatch) -> None:
    user = UserFactory()
    _, codes = _enroll(client, user)
    token = _challenge(client, user)
    with monkeypatch.context() as context:
        context.setattr(settings, "JWT_SIGNING_KEY", "")
        assert _complete(client, token, codes[0]).status_code == 503
    assert not MFARecoveryCode.objects.filter(consumed_at__isnull=False).exists()
    assert MFAChallenge.objects.get(consumed_at__isnull=True)
    assert not UserSession.objects.filter(revoked_at__isnull=True).exists()
    assert _complete(client, token, codes[0]).status_code == 200


def test_wrong_decryption_key_fails_but_recovery_code_still_works(client, monkeypatch) -> None:
    user = UserFactory()
    seed, codes = _enroll(client, user)
    token = _challenge(client, user)
    monkeypatch.setattr(settings, "MFA_ENCRYPTION_KEYS", Fernet.generate_key().decode())
    assert _complete(client, token, _code(seed)).status_code == 503
    assert _complete(client, token, codes[0]).status_code == 200


def test_sms_delivery_failure_keeps_recovery_code_login_available(client, monkeypatch) -> None:
    user = UserFactory(is_phone_verified=True, phone=f"+234803{uuid.uuid4().int % 10**7:07d}")
    _, codes = _enroll(client, user, "SMS")

    def unavailable():
        raise DeliveryUnavailable()

    monkeypatch.setattr(mfa, "get_backend", unavailable)
    response = _post(client, "login", identifier=user.phone, password=FACTORY_PASSWORD)
    assert response.status_code == 200
    assert response.json()["data"]["sms_sent"] is False
    assert not MFAChallenge.objects.get(consumed_at__isnull=True).code_hash
    assert _complete(client, response.json()["data"]["mfa_token"], codes[0]).status_code == 200


@pytest.mark.parametrize("scope", ["mfa_setup", "mfa_verify", "mfa_challenge_ip"])
def test_mfa_limits_fail_with_retry_after(client, monkeypatch, scope) -> None:
    user = UserFactory()
    _, codes = _enroll(client, user)
    token = _challenge(client, user)
    _bearer(client, _complete(client, token, codes[0]).json()["data"]["access_token"])

    def consume(name, identifier):
        return ratelimit.Decision(name, name != scope, 10, 0, 37)

    monkeypatch.setattr(ratelimit, "consume", consume)
    route, body = (
        ("mfa/disable", {"current_password": FACTORY_PASSWORD, "code": codes[1]})
        if scope == "mfa_setup"
        else ("mfa/challenge", {"mfa_token": token, "code": codes[1]})
    )
    response = _post(client, route, **body)
    assert response.status_code == 429 and response["Retry-After"] == "37"


def test_disable_needs_password_and_factor_and_ends_all_sessions(client) -> None:
    user = UserFactory()
    _, codes = _enroll(client, user)
    token = _challenge(client, user)
    pair = _complete(client, token, codes[0]).json()["data"]
    _bearer(client, pair["access_token"])
    assert (
        _post(client, "mfa/disable", current_password="incorrect", code=codes[1]).status_code == 401
    )
    assert (
        _post(
            client, "mfa/disable", current_password=FACTORY_PASSWORD, code="ffffffffffffffffffff"
        ).status_code
        == 400
    )
    assert (
        _post(client, "mfa/disable", current_password=FACTORY_PASSWORD, code=codes[1]).status_code
        == 204
    )
    user.refresh_from_db()
    assert not user.two_factor_enabled and user.totp_secret == "" and user.mfa_version == 2
    assert not MFARecoveryCode.objects.exists()
    assert client.get(f"{AUTH}sessions/").status_code == 401
    assert _post(client, "token/refresh", refresh_token=pair["refresh_token"]).status_code == 401
    client.credentials()
    assert "access_token" in _login(client, user)


def test_encrypted_seeds_rotate_without_resetting_authenticator_or_session_hash(
    client, monkeypatch
) -> None:
    active = UserFactory()
    seed, _ = _enroll(client, active)
    pending_user = UserFactory()
    _bearer(client, _login(client, pending_user)["access_token"])
    pending = _begin(client, pending_user)["secret"]
    active.refresh_from_db()
    before_hash = active.get_session_auth_hash()
    old_key = settings.MFA_ENCRYPTION_KEYS
    new_key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "MFA_ENCRYPTION_KEYS", f"{new_key},{old_key}")
    call_command("rotate_mfa_keys", stdout=io.StringIO())
    monkeypatch.setattr(settings, "MFA_ENCRYPTION_KEYS", new_key)
    active.refresh_from_db()
    assert mfa.decrypt(active.totp_secret) == seed
    assert active.get_session_auth_hash() == before_hash
    assert mfa.decrypt(MFAEnrollment.objects.get(user=pending_user).secret) == pending
    with pytest.raises(InvalidToken):
        Fernet(old_key.encode()).decrypt(active.totp_secret.encode())


@pytest.mark.parametrize("flags", [{"two_factor_enabled": True}, {"is_staff": True}])
def test_django_password_cookie_cannot_bypass_mfa(client, flags) -> None:
    user = UserFactory(**flags)
    client.force_login(user)
    assert client.get(f"{AUTH}sessions/").status_code == 401


def test_browser_cookie_dies_when_mfa_is_confirmed(client) -> None:
    user = UserFactory()
    client.force_login(user)
    setup = _begin(client, user)
    assert _post(client, "mfa/confirm", code=_code(setup["secret"])).status_code == 200
    assert client.get(f"{AUTH}sessions/").status_code == 401


def test_cookie_mfa_mutations_require_csrf() -> None:
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(UserFactory())
    assert (
        _post(client, "mfa/enable", method="TOTP", current_password=FACTORY_PASSWORD).status_code
        == 403
    )


def test_unknown_challenge_and_unsupported_legacy_factor_fail_closed(client) -> None:
    assert _complete(client, "unknown", "000000").status_code == 401
    user = UserFactory(two_factor_enabled=True, two_factor_method="EMAIL")
    assert (
        _post(client, "login", identifier=user.phone, password=FACTORY_PASSWORD).status_code == 503
    )


def test_setup_proof_cannot_be_used_as_refresh_token(client) -> None:
    token = _challenge(client, UserFactory(is_staff=True))
    assert _post(client, "token/refresh", refresh_token=token).status_code == 401


def test_admin_password_login_is_staged_and_existing_staff_cookie_is_blocked(client) -> None:
    user = UserFactory(is_staff=True)
    response = client.post("/admin/login/", {"username": user.phone, "password": FACTORY_PASSWORD})
    assert response.status_code == 302 and response["Location"] == "/admin/mfa/"
    assert "_auth_user_id" not in client.session
    client.force_login(user)
    response = client.get("/admin/")
    assert response.status_code == 302 and response["Location"].startswith("/admin/login/")


def test_malformed_encrypted_seed_fails_with_service_unavailable(client) -> None:
    user = UserFactory()
    _enroll(client, user)
    User.objects.filter(pk=user.pk).update(totp_secret=mfa.encrypt("invalid-base32"))
    assert _complete(client, _challenge(client, user), "000000").status_code == 503


@pytest.mark.parametrize(
    "timestamp,expected",
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
    ],
)
def test_totp_matches_rfc_6238_sha1_vectors(timestamp, expected) -> None:
    seed = base64.b32encode(b"12345678901234567890").decode()
    assert mfa.totp(seed, timestamp // 30, digits=8) == expected
