"""Browser and mobile issuance share a single-use factor under a user lock."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.cached_db import SessionStore
from django.db import close_old_connections
from django.test import RequestFactory
from django.utils import timezone

from apps.accounts.constants import SessionKind
from apps.accounts.models import MFAChallenge, MFARecoveryCode, UserSession
from apps.accounts.services import browser, mfa
from apps.accounts.services.errors import InvalidCredentials, InvalidOTP
from tests.api.test_authentication_api import _keys
from tests.factories import DeviceFactory, UserFactory
from tests.factories.accounts import FACTORY_PASSWORD

pytestmark = [
    pytest.mark.concurrency,
    pytest.mark.security,
    pytest.mark.django_db(transaction=True),
]


@pytest.mark.parametrize("factor", ["totp", "recovery"])
@pytest.mark.parametrize("peer", ["browser", "mobile"])
def test_concurrent_browser_factor_issuance_has_one_winner(monkeypatch, factor, peer):
    private, public = _keys()
    monkeypatch.setattr(settings, "JWT_SIGNING_KEY", private)
    monkeypatch.setattr(settings, "JWT_VERIFYING_KEY", public)
    monkeypatch.setattr(settings, "MFA_ENCRYPTION_KEYS", Fernet.generate_key().decode())
    user = UserFactory(is_staff=True)
    seed = mfa.begin(user=user, method="TOTP", current_password=FACTORY_PASSWORD)["secret"]
    step = int(timezone.now().timestamp()) // 30
    codes = mfa.confirm(user=user, code=mfa.totp(seed, step - 1))
    user.refresh_from_db()
    cookie = SessionStore()
    cookie.create()
    browser_token, mobile_token = uuid4().hex, uuid4().hex
    cookie[browser.PENDING_TOKEN] = browser_token
    cookie.save()
    MFAChallenge.objects.create(
        user=user,
        device=DeviceFactory(user=user),
        audience=SessionKind.BROWSER,
        browser_session_key_hash=mfa.digest(cookie.session_key, "browser"),
        token_hash=mfa.digest(browser_token, "challenge"),
        credential_state=mfa.state(user),
        expires_at=timezone.now() + mfa.CHALLENGE_TTL,
    )
    if peer == "mobile":
        MFAChallenge.objects.create(
            user=user,
            device=DeviceFactory(user=user),
            token_hash=mfa.digest(mobile_token, "challenge"),
            credential_state=mfa.state(user),
            expires_at=timezone.now() + mfa.CHALLENGE_TTL,
        )
    proof = codes[0] if factor == "recovery" else mfa.totp(seed, step)
    barrier = Barrier(2)

    def complete(is_browser):
        close_old_connections()
        try:
            request = RequestFactory().post(
                "/admin/mfa/", REMOTE_ADDR=f"2001:db8::{uuid4().int % 65535:x}"
            )
            request.user = AnonymousUser()
            request.session = SessionStore(session_key=cookie.session_key)
            # Load each anonymous snapshot before either worker rotates the key.
            assert request.session[browser.PENDING_TOKEN] == browser_token
            barrier.wait(timeout=10)
            try:
                if is_browser:
                    browser.finish(request, code=proof)
                else:
                    mfa.complete(
                        mfa_token=mobile_token, code=proof, source_ip=request.META["REMOTE_ADDR"]
                    )
            except (InvalidCredentials, InvalidOTP):
                return False
            return True
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(complete, True), executor.submit(complete, peer == "browser")]
        outcomes = [job.result(timeout=20) for job in jobs]
    assert outcomes.count(True) == 1
    assert UserSession.objects.filter(revoked_at__isnull=True, mfa_verified=True).count() == 1
    if factor == "recovery":
        assert MFARecoveryCode.objects.filter(consumed_at__isnull=False).count() == 1
