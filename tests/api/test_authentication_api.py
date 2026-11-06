"""S4 HTTP contracts and security boundaries, using real PostgreSQL and Redis."""

from __future__ import annotations

import ipaddress
import logging
import uuid
from datetime import timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.constants import AccountStatus, DevicePlatform, SessionRevocationReason
from apps.accounts.models import Device, User, UserSession
from apps.accounts.services import sessions
from config import ratelimit
from tests.factories import DeviceFactory, UserFactory
from tests.factories.accounts import FACTORY_PASSWORD

pytestmark = [pytest.mark.api, pytest.mark.security, pytest.mark.django_db]

AUTH = "/api/v1/auth/"


def _keys(bits: int = 2048) -> tuple[str, str]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    return (
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode(),
    )


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[str, str]:
    return _keys()


@pytest.fixture(autouse=True)
def _configure_keys(monkeypatch, rsa_keys: tuple[str, str]) -> None:
    monkeypatch.setattr(settings, "JWT_SIGNING_KEY", rsa_keys[0])
    monkeypatch.setattr(settings, "JWT_VERIFYING_KEY", rsa_keys[1])


@pytest.fixture
def client() -> APIClient:
    # An independent documentation-only IPv6 address also isolates Redis's
    # IP counters across tests and repeated test runs.
    address = ipaddress.IPv6Address(
        int(ipaddress.IPv6Address("2001:db8::")) + (uuid.uuid4().int >> 64)
    )
    return APIClient(REMOTE_ADDR=str(address), HTTP_USER_AGENT="IHWEZA test client")


def _login(client: APIClient, user: User, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "identifier": user.phone,
        "password": FACTORY_PASSWORD,
        "device": {"fingerprint": str(uuid.uuid4()), "platform": DevicePlatform.ANDROID},
    }
    body.update(overrides)
    response = client.post(f"{AUTH}login/", body, format="json")
    assert response.status_code == 200, response.json()
    return response.json()["data"]


def _bearer(client: APIClient, token: str) -> None:
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")


def _claims(token: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        settings.JWT_VERIFYING_KEY,
        algorithms=["RS256"],
        issuer=settings.JWT_ISSUER,
        audience=settings.JWT_AUDIENCE,
    )


class TestLogin:
    def test_tokens_are_rsa_signed_device_bound_and_contain_only_public_claims(
        self, client: APIClient
    ) -> None:
        user = UserFactory()
        pair = _login(client, user)
        claims = _claims(pair["access_token"])
        session = UserSession.objects.get(public_id=claims["sid"])
        assert jwt.get_unverified_header(pair["access_token"])["alg"] == "RS256"
        assert set(claims) == {"sub", "sid", "iat", "exp", "jti", "iss", "aud", "token_type"}
        assert claims["sub"] == str(user.public_id)
        assert claims["exp"] - claims["iat"] == pair["expires_in"] == 900
        assert pair["user"] == {
            "id": str(user.public_id),
            "full_name": user.full_name,
            "mfa_required": False,
        }
        assert pair["device_id"] == str(session.device.public_id)
        assert session.device.user_id == user.pk
        assert len(pair["refresh_token"]) == 64
        assert pair["refresh_token"] not in session.refresh_token_hash
        assert user.phone not in str(claims)
        assert user.email not in str(claims)

    def test_email_login_is_case_insensitive(self, client: APIClient) -> None:
        user = UserFactory(email="Ada@Example.test")
        pair = _login(client, user, identifier="ada@example.TEST")
        assert pair["user"]["id"] == str(user.public_id)

    def test_password_whitespace_is_preserved(self, client: APIClient) -> None:
        password = "  factory-password-with-spaces  "  # pragma: allowlist secret
        user = UserFactory(password=password)
        assert _login(client, user, password=password)["user"]["id"] == str(user.public_id)

    def test_unknown_account_and_wrong_password_have_identical_errors(
        self, client: APIClient
    ) -> None:
        user = UserFactory()
        responses = [
            client.post(
                f"{AUTH}login/",
                {"identifier": identifier, "password": "incorrect"},
                format="json",
            )
            for identifier in ("unknown@example.test", user.phone)
        ]
        assert [r.status_code for r in responses] == [401, 401]
        errors = [r.json()["error"] for r in responses]
        for error in errors:
            assert error.pop("request_id")
        assert errors[0] == errors[1]
        assert errors[0]["code"] == "authentication_required"
        assert not UserSession.objects.exists()

    @pytest.mark.parametrize("status", [AccountStatus.SUSPENDED, AccountStatus.CLOSED])
    def test_account_status_is_reported_only_after_password_proof(
        self, client: APIClient, status: str
    ) -> None:
        user = UserFactory(account_status=status)
        wrong = client.post(
            f"{AUTH}login/",
            {"identifier": user.phone, "password": "incorrect"},
            format="json",
        )
        correct = client.post(
            f"{AUTH}login/",
            {"identifier": user.phone, "password": FACTORY_PASSWORD},
            format="json",
        )
        assert wrong.status_code == 401
        assert correct.status_code == 403
        assert correct.json()["error"]["code"] == f"account_{status.lower()}"
        assert not UserSession.objects.exists()

    @pytest.mark.parametrize(
        "flags", [{"two_factor_enabled": True}, {"is_staff": True}, {"is_superuser": True}]
    )
    def test_mfa_accounts_cannot_receive_password_only_tokens(
        self, client: APIClient, flags: dict[str, bool]
    ) -> None:
        user = UserFactory(**flags)
        response = client.post(
            f"{AUTH}login/",
            {"identifier": user.phone, "password": FACTORY_PASSWORD},
            format="json",
        )
        if flags.get("two_factor_enabled"):
            # Legacy enabled accounts with no supported method fail closed.
            assert response.status_code == 503
        else:
            assert response.status_code == 200
            assert response.json()["data"]["enrollment_required"] is True
            assert "access_token" not in response.json()["data"]
        assert not UserSession.objects.exists()
        assert Device.objects.count() == (0 if flags.get("two_factor_enabled") else 1)

    def test_a_device_uuid_can_be_reused_without_overwriting_its_metadata(
        self, client: APIClient
    ) -> None:
        user = UserFactory()
        first = _login(
            client,
            user,
            device={"fingerprint": "test-handset", "platform": "ANDROID", "model": "Tecno Spark"},
        )
        second = _login(client, user, device={"id": first["device_id"]})
        first_session = UserSession.objects.get(public_id=_claims(first["access_token"])["sid"])
        second_session = UserSession.objects.get(public_id=_claims(second["access_token"])["sid"])
        assert first["device_id"] == second["device_id"]
        assert Device.objects.count() == 1
        assert second_session.device.model == "Tecno Spark"
        assert second_session.device.platform == "ANDROID"
        assert first_session.family_id != second_session.family_id

    def test_a_fingerprint_is_owner_scoped(self, client: APIClient) -> None:
        body = {"fingerprint": "shared-client-name", "platform": "IOS"}
        first = _login(client, UserFactory(), device=body)
        second = _login(client, UserFactory(), device=body)
        assert first["device_id"] != second["device_id"]

    def test_device_metadata_can_be_updated(self, client: APIClient) -> None:
        user = UserFactory()
        first = _login(client, user)
        _login(
            client,
            user,
            device={
                "id": first["device_id"],
                "platform": "IOS",
                "model": "iPhone",
                "os_version": "18",
                "app_version": "1.2",
            },
        )
        device = Device.objects.get(public_id=first["device_id"])
        assert (device.platform, device.model, device.os_version, device.app_version) == (
            "IOS",
            "iPhone",
            "18",
            "1.2",
        )

    def test_omitting_a_device_creates_a_web_device(self, client: APIClient) -> None:
        user = UserFactory()
        response = client.post(
            f"{AUTH}login/",
            {"identifier": user.phone, "password": FACTORY_PASSWORD},
            format="json",
        )
        assert response.status_code == 200
        device = Device.objects.get(public_id=response.json()["data"]["device_id"])
        assert device.platform == "WEB"

    def test_another_members_device_id_cannot_be_claimed(self, client: APIClient) -> None:
        device = DeviceFactory()
        user = UserFactory()
        response = client.post(
            f"{AUTH}login/",
            {
                "identifier": user.phone,
                "password": FACTORY_PASSWORD,
                "device": {"id": str(device.public_id)},
            },
            format="json",
        )
        assert response.status_code == 401
        assert not UserSession.objects.exists()

    @pytest.mark.parametrize("by_id", [True, False])
    def test_a_revoked_device_is_not_silently_reactivated(
        self, client: APIClient, by_id: bool
    ) -> None:
        device = DeviceFactory(is_revoked=True)
        identification = (
            {"id": str(device.public_id)} if by_id else {"fingerprint": device.device_fingerprint}
        )
        response = client.post(
            f"{AUTH}login/",
            {
                "identifier": device.user.phone,
                "password": FACTORY_PASSWORD,
                "device": identification,
            },
            format="json",
        )
        assert response.status_code == 401
        device.refresh_from_db()
        assert device.is_revoked
        assert not UserSession.objects.exists()

    @pytest.mark.parametrize("setting", ["JWT_SIGNING_KEY", "JWT_VERIFYING_KEY"])
    def test_missing_keys_fail_closed_without_orphan_sessions(
        self, client: APIClient, monkeypatch, setting: str
    ) -> None:
        user = UserFactory()
        monkeypatch.setattr(settings, setting, "")
        response = client.post(
            f"{AUTH}login/",
            {"identifier": user.phone, "password": FACTORY_PASSWORD},
            format="json",
        )
        assert response.status_code == 503
        assert not UserSession.objects.exists()
        assert not Device.objects.exists()

    @pytest.mark.parametrize("weak", [False, True])
    def test_mismatched_or_weak_keypairs_cannot_sign(
        self, client: APIClient, monkeypatch, weak: bool
    ) -> None:
        private, public = _keys(1024 if weak else 2048)
        if weak:
            monkeypatch.setattr(settings, "JWT_SIGNING_KEY", private)
        monkeypatch.setattr(settings, "JWT_VERIFYING_KEY", public)
        user = UserFactory()
        response = client.post(
            f"{AUTH}login/",
            {"identifier": user.phone, "password": FACTORY_PASSWORD},
            format="json",
        )
        assert response.status_code == 503
        assert not UserSession.objects.exists()

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"identifier": "a@example.test"},
            {"password": "incorrect"},
            {"identifier": "a@example.test", "password": ""},
            {
                "identifier": "a@example.test",
                "password": "incorrect",
                "device": {"id": str(uuid.uuid4()), "fingerprint": "ambiguous"},
            },
            {
                "identifier": "a@example.test",
                "password": "incorrect",
                "device": {"platform": "INVALID"},
            },
        ],
    )
    def test_malformed_input_uses_the_validation_envelope(
        self, client: APIClient, body: dict[str, Any]
    ) -> None:
        response = client.post(f"{AUTH}login/", body, format="json")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_error"
        assert response.json()["error"]["details"]

    @pytest.mark.parametrize("scope", ["login_ip", "login"])
    def test_login_limits_return_retry_after(
        self, client: APIClient, monkeypatch, scope: str
    ) -> None:
        def consume(name: str, identifier: str) -> ratelimit.Decision:
            return ratelimit.Decision(name, name != scope, 10, 0, 37)

        monkeypatch.setattr(ratelimit, "consume", consume)
        response = client.post(
            f"{AUTH}login/",
            {"identifier": "unknown@example.test", "password": "incorrect"},
            format="json",
        )
        assert response.status_code == 429
        assert response["Retry-After"] == "37"
        assert response.json()["error"]["code"] == "rate_limited"
        assert not UserSession.objects.exists()

    def test_redis_outage_is_a_503_and_never_unthrottled(
        self, client: APIClient, monkeypatch
    ) -> None:
        def unavailable(*args, **kwargs) -> None:
            raise ratelimit.RateLimiterUnavailable("login")

        monkeypatch.setattr(ratelimit, "consume", unavailable)
        response = client.post(
            f"{AUTH}login/",
            {"identifier": "unknown@example.test", "password": "incorrect"},
            format="json",
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "service_unavailable"
        assert not UserSession.objects.exists()

    def test_credentials_never_reach_logs(self, client: APIClient, caplog) -> None:
        user = UserFactory()
        with caplog.at_level(logging.INFO):
            pair = _login(client, user)
        records = str([record.__dict__ for record in caplog.records])
        for secret in (FACTORY_PASSWORD, pair["access_token"], pair["refresh_token"]):
            assert secret not in records


class TestRefresh:
    def test_rotation_returns_new_tokens_and_keeps_the_binding(self, client: APIClient) -> None:
        user = UserFactory()
        before = _login(client, user)
        response = client.post(
            f"{AUTH}token/refresh/", {"refresh_token": before["refresh_token"]}, format="json"
        )
        assert response.status_code == 200
        after = response.json()["data"]
        old = UserSession.objects.get(public_id=_claims(before["access_token"])["sid"])
        new = UserSession.objects.get(public_id=_claims(after["access_token"])["sid"])
        assert after["access_token"] != before["access_token"]
        assert after["refresh_token"] != before["refresh_token"]
        assert after["device_id"] == before["device_id"]
        assert old.family_id == new.family_id
        assert old.revoked_reason == SessionRevocationReason.ROTATED
        # In-flight access tokens remain valid until their 15-minute expiry
        # while the rotation family is active.
        _bearer(client, before["access_token"])
        assert client.get(f"{AUTH}sessions/").status_code == 200

    def test_http_reuse_revocation_is_committed_and_blocks_the_successor(
        self, client: APIClient
    ) -> None:
        original = _login(client, UserFactory())
        rotated = client.post(
            f"{AUTH}token/refresh/", {"refresh_token": original["refresh_token"]}, format="json"
        ).json()["data"]
        replay = client.post(
            f"{AUTH}token/refresh/", {"refresh_token": original["refresh_token"]}, format="json"
        )
        assert replay.status_code == 401
        assert replay.json()["error"]["code"] == "authentication_required"
        _bearer(client, rotated["access_token"])
        assert client.get(f"{AUTH}sessions/").status_code == 401
        assert not UserSession.objects.filter(revoked_at__isnull=True).exists()

    def test_old_logout_does_not_end_a_new_login_on_the_same_device(
        self, client: APIClient, caplog
    ) -> None:
        user = UserFactory()
        old = _login(client, user)
        _bearer(client, old["access_token"])
        assert client.post(f"{AUTH}logout/").status_code == 204
        current = _login(client, user, device={"id": old["device_id"]})
        response = client.post(
            f"{AUTH}token/refresh/", {"refresh_token": old["refresh_token"]}, format="json"
        )
        assert response.status_code == 401
        assert not any(r.getMessage() == "session.refresh_reuse_detected" for r in caplog.records)
        _bearer(client, current["access_token"])
        assert client.get(f"{AUTH}sessions/").status_code == 200

    def test_logout_after_rotation_clears_the_predecessor_replay_marker(
        self, client: APIClient, caplog
    ) -> None:
        first = _login(client, UserFactory())
        current = client.post(
            f"{AUTH}token/refresh/", {"refresh_token": first["refresh_token"]}, format="json"
        ).json()["data"]
        _bearer(client, current["access_token"])
        assert client.post(f"{AUTH}logout/").status_code == 204
        assert (
            client.post(
                f"{AUTH}token/refresh/", {"refresh_token": first["refresh_token"]}, format="json"
            ).status_code
            == 401
        )
        assert not any(r.getMessage() == "session.refresh_reuse_detected" for r in caplog.records)

    @pytest.mark.parametrize("status", [AccountStatus.SUSPENDED, AccountStatus.CLOSED, "UNKNOWN"])
    def test_inactive_accounts_cannot_use_either_token(
        self, client: APIClient, status: str
    ) -> None:
        user = UserFactory()
        pair = _login(client, user)
        User.objects.filter(pk=user.pk).update(account_status=status)
        _bearer(client, pair["access_token"])
        assert client.get(f"{AUTH}sessions/").status_code == 401
        assert (
            client.post(
                f"{AUTH}token/refresh/", {"refresh_token": pair["refresh_token"]}, format="json"
            ).status_code
            == 401
        )

    def test_a_device_revoked_outside_the_api_is_still_enforced(self, client: APIClient) -> None:
        pair = _login(client, UserFactory())
        Device.objects.filter(public_id=pair["device_id"]).update(is_revoked=True)
        _bearer(client, pair["access_token"])
        assert client.get(f"{AUTH}sessions/").status_code == 401
        assert (
            client.post(
                f"{AUTH}token/refresh/", {"refresh_token": pair["refresh_token"]}, format="json"
            ).status_code
            == 401
        )

    def test_enabling_mfa_stops_password_only_sessions(self, client: APIClient) -> None:
        user = UserFactory()
        pair = _login(client, user)
        User.objects.filter(pk=user.pk).update(two_factor_enabled=True)
        _bearer(client, pair["access_token"])
        assert client.get(f"{AUTH}sessions/").status_code == 401
        assert (
            client.post(
                f"{AUTH}token/refresh/", {"refresh_token": pair["refresh_token"]}, format="json"
            ).status_code
            == 401
        )
        assert not UserSession.objects.filter(revoked_at__isnull=True).exists()

    def test_signing_failure_does_not_spend_the_refresh_token(
        self, client: APIClient, monkeypatch
    ) -> None:
        pair = _login(client, UserFactory())
        with monkeypatch.context() as context:
            context.setattr(settings, "JWT_SIGNING_KEY", "")
            response = client.post(
                f"{AUTH}token/refresh/", {"refresh_token": pair["refresh_token"]}, format="json"
            )
        assert response.status_code == 503
        assert UserSession.objects.count() == 1
        assert UserSession.objects.get().revoked_at is None
        assert (
            client.post(
                f"{AUTH}token/refresh/", {"refresh_token": pair["refresh_token"]}, format="json"
            ).status_code
            == 200
        )

    def test_expired_tokens_are_rejected_and_marked_expired(self, client: APIClient) -> None:
        pair = _login(client, UserFactory())
        stamp = timezone.now()
        UserSession.objects.update(
            issued_at=stamp - timedelta(days=1), expires_at=stamp - timedelta(seconds=1)
        )
        assert (
            client.post(
                f"{AUTH}token/refresh/", {"refresh_token": pair["refresh_token"]}, format="json"
            ).status_code
            == 401
        )
        assert UserSession.objects.get().revoked_reason == SessionRevocationReason.EXPIRED

    def test_refresh_limits_run_before_database_queries(
        self, client: APIClient, monkeypatch, django_assert_num_queries
    ) -> None:
        monkeypatch.setattr(
            ratelimit,
            "consume",
            lambda scope, identifier: ratelimit.Decision(scope, False, 120, 0, 19),
        )
        with django_assert_num_queries(0):
            response = client.post(
                f"{AUTH}token/refresh/", {"refresh_token": "fabricated"}, format="json"
            )
        assert response.status_code == 429
        assert response["Retry-After"] == "19"

    @pytest.mark.parametrize("body", [{}, {"refresh_token": ""}, {"refresh_token": None}])
    def test_malformed_refresh_input_is_a_validation_error(
        self, client: APIClient, body: dict[str, Any]
    ) -> None:
        response = client.post(f"{AUTH}token/refresh/", body, format="json")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_error"

    def test_an_unknown_refresh_token_is_not_authenticated(self, client: APIClient) -> None:
        response = client.post(
            f"{AUTH}token/refresh/", {"refresh_token": "fabricated"}, format="json"
        )
        assert response.status_code == 401


class TestSessionAndDeviceManagement:
    def test_lists_are_owner_scoped_and_contain_no_credentials(self, client: APIClient) -> None:
        owner = _login(client, UserFactory())
        other = _login(client, UserFactory())
        _bearer(client, owner["access_token"])
        session_body = client.get(f"{AUTH}sessions/").json()
        device_body = client.get(f"{AUTH}devices/").json()
        assert [row["id"] for row in session_body["data"]] == [
            _claims(owner["access_token"])["sid"]
        ]
        assert [row["id"] for row in device_body["data"]] == [owner["device_id"]]
        for body in (session_body, device_body):
            assert body["pagination"]["has_more"] is False
            text = str(body)
            for hidden in ("refresh_token_hash", "password", "push_token", "device_fingerprint"):
                assert hidden not in text
            assert other["device_id"] not in text

    def test_lists_are_cursor_paginated(self, client: APIClient) -> None:
        user = UserFactory()
        for _ in range(3):
            pair = _login(client, user)
        _bearer(client, pair["access_token"])
        first = client.get(f"{AUTH}sessions/?limit=1").json()
        assert len(first["data"]) == 1
        assert first["pagination"]["has_more"]
        cursor = first["pagination"]["next_cursor"]
        second = client.get(f"{AUTH}sessions/?limit=1&cursor={cursor}").json()
        assert first["data"][0]["id"] != second["data"][0]["id"]
        assert len(client.get(f"{AUTH}devices/?limit=1").json()["data"]) == 1

    def test_revoke_session_ends_only_its_login_chain(self, client: APIClient) -> None:
        user = UserFactory()
        target = _login(client, user)
        keeper = _login(client, user, device={"id": target["device_id"]})
        _bearer(client, keeper["access_token"])
        session_id = _claims(target["access_token"])["sid"]
        assert client.delete(f"{AUTH}sessions/{session_id}/").status_code == 204
        assert client.delete(f"{AUTH}sessions/{session_id}/").status_code == 204
        assert client.get(f"{AUTH}sessions/").status_code == 200
        assert (
            client.post(
                f"{AUTH}token/refresh/", {"refresh_token": target["refresh_token"]}, format="json"
            ).status_code
            == 401
        )

    @pytest.mark.parametrize("resource", ["sessions", "devices"])
    def test_foreign_resources_and_unknown_ids_have_the_same_404(
        self, client: APIClient, resource: str
    ) -> None:
        owner = _login(client, UserFactory())
        other = _login(client, UserFactory())
        _bearer(client, owner["access_token"])
        foreign_id = (
            _claims(other["access_token"])["sid"] if resource == "sessions" else other["device_id"]
        )
        foreign = client.delete(f"{AUTH}{resource}/{foreign_id}/")
        unknown = client.delete(f"{AUTH}{resource}/{uuid.uuid4()}/")
        assert foreign.status_code == unknown.status_code == 404
        assert foreign.json()["error"]["code"] == unknown.json()["error"]["code"] == "not_found"
        _bearer(client, other["access_token"])
        assert client.get(f"{AUTH}sessions/").status_code == 200

    def test_device_revocation_ends_all_its_families_and_preserves_other_devices(
        self, client: APIClient
    ) -> None:
        user = UserFactory()
        lost = _login(client, user)
        another_family = _login(client, user, device={"id": lost["device_id"]})
        keeper = _login(client, user)
        _bearer(client, keeper["access_token"])
        assert client.delete(f"{AUTH}devices/{lost['device_id']}/").status_code == 204
        assert client.delete(f"{AUTH}devices/{lost['device_id']}/").status_code == 204
        assert client.get(f"{AUTH}sessions/").status_code == 200
        assert Device.objects.get(public_id=lost["device_id"]).is_revoked
        for pair in (lost, another_family):
            assert (
                client.post(
                    f"{AUTH}token/refresh/", {"refresh_token": pair["refresh_token"]}, format="json"
                ).status_code
                == 401
            )

    def test_revoking_the_current_device_invalidates_access_immediately(
        self, client: APIClient
    ) -> None:
        pair = _login(client, UserFactory())
        _bearer(client, pair["access_token"])
        assert client.delete(f"{AUTH}devices/{pair['device_id']}/").status_code == 204
        assert client.get(f"{AUTH}sessions/").status_code == 401

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "sessions/"),
            ("get", "devices/"),
            ("post", "logout/"),
            ("delete", f"sessions/{uuid.uuid4()}/"),
            ("delete", f"devices/{uuid.uuid4()}/"),
        ],
    )
    def test_management_requires_authentication(
        self, client: APIClient, method: str, path: str
    ) -> None:
        response = getattr(client, method)(f"{AUTH}{path}")
        assert response.status_code == 401
        assert response["WWW-Authenticate"] == "Bearer"
        assert response.json()["error"]["code"] == "authentication_required"

    def test_cookie_logout_requires_csrf(self) -> None:
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(UserFactory())
        assert client.post(f"{AUTH}logout/").status_code == 403

    def test_cookie_logout_clears_the_django_session(self) -> None:
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(UserFactory())
        client.get("/admin/login/")
        csrf = client.cookies["csrftoken"].value
        assert client.post(f"{AUTH}logout/", HTTP_X_CSRFTOKEN=csrf).status_code == 204
        assert client.get(f"{AUTH}sessions/").status_code == 401


class TestAccessTokenValidation:
    @pytest.mark.parametrize("algorithm", ["HS256", "none"])
    def test_hmac_and_unsigned_tokens_cannot_bypass_rsa(
        self, client: APIClient, algorithm: str
    ) -> None:
        pair = _login(client, UserFactory())
        forged = jwt.encode(
            _claims(pair["access_token"]),
            settings.SECRET_KEY if algorithm == "HS256" else None,
            algorithm=algorithm,
        )
        _bearer(client, forged)
        assert client.get(f"{AUTH}sessions/").status_code == 401

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("iss", "wrong-issuer"),
            ("aud", "wrong-audience"),
            ("token_type", "refresh"),
            ("sub", "not-a-uuid"),
            ("sid", "not-a-uuid"),
            ("sub", str(uuid.uuid4())),
            ("sid", str(uuid.uuid4())),
            ("iat", 4102444800),
            ("exp", 1),
        ],
    )
    def test_invalid_claims_cannot_authenticate(
        self, client: APIClient, field: str, value: Any
    ) -> None:
        pair = _login(client, UserFactory())
        claims = _claims(pair["access_token"])
        claims[field] = value
        forged = jwt.encode(claims, settings.JWT_SIGNING_KEY, algorithm="RS256")
        _bearer(client, forged)
        response = client.get(f"{AUTH}sessions/")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_required"

    @pytest.mark.parametrize("field", ["sub", "sid", "iat", "exp", "jti", "token_type"])
    def test_required_claims_cannot_be_omitted(self, client: APIClient, field: str) -> None:
        pair = _login(client, UserFactory())
        claims = _claims(pair["access_token"])
        del claims[field]
        _bearer(client, jwt.encode(claims, settings.JWT_SIGNING_KEY, algorithm="RS256"))
        assert client.get(f"{AUTH}sessions/").status_code == 401

    def test_a_session_uuid_cannot_be_transplanted_between_members(self, client: APIClient) -> None:
        owner = _login(client, UserFactory())
        other = _login(client, UserFactory())
        claims = _claims(owner["access_token"])
        claims["sid"] = _claims(other["access_token"])["sid"]
        _bearer(client, jwt.encode(claims, settings.JWT_SIGNING_KEY, algorithm="RS256"))
        assert client.get(f"{AUTH}sessions/").status_code == 401

    @pytest.mark.parametrize("header", ["Bearer", "Bearer a b", "Bearer fabricated", "Bearer ÿ"])
    def test_malformed_bearer_headers_are_authentication_errors(
        self, client: APIClient, header: str
    ) -> None:
        client.credentials(HTTP_AUTHORIZATION=header)
        response = client.get(f"{AUTH}sessions/")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_required"

    def test_an_unbound_legacy_session_cannot_authorize_mobile_access(
        self, client: APIClient
    ) -> None:
        from apps.accounts.services.tokens import issue_access

        session = sessions.issue(user=UserFactory()).session
        _bearer(client, issue_access(session))
        assert client.get(f"{AUTH}sessions/").status_code == 401

    def test_activity_is_recorded_on_authenticated_use(self, client: APIClient) -> None:
        pair = _login(client, UserFactory())
        _bearer(client, pair["access_token"])
        assert client.get(f"{AUTH}sessions/").status_code == 200
        session = UserSession.objects.get(public_id=_claims(pair["access_token"])["sid"])
        assert session.last_used_at is not None
        assert session.device.last_seen_at is not None
