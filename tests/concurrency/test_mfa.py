"""Concurrent challenges cannot spend a second-factor proof twice."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from apps.accounts.models import MFAChallenge, MFARecoveryCode, UserSession
from apps.accounts.services import mfa
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
@pytest.mark.parametrize("same_challenge", [True, False])
def test_second_factor_proof_has_one_concurrent_winner(monkeypatch, factor, same_challenge) -> None:
    private, public = _keys()
    monkeypatch.setattr(settings, "JWT_SIGNING_KEY", private)
    monkeypatch.setattr(settings, "JWT_VERIFYING_KEY", public)
    monkeypatch.setattr(settings, "MFA_ENCRYPTION_KEYS", Fernet.generate_key().decode())
    user = UserFactory()
    seed = mfa.begin(user=user, method="TOTP", current_password=FACTORY_PASSWORD)["secret"]
    step = int(timezone.now().timestamp()) // 30
    codes = mfa.confirm(user=user, code=mfa.totp(seed, step - 1))
    user.refresh_from_db()
    tokens = [uuid4().hex, uuid4().hex]
    for token in tokens[:1] if same_challenge else tokens:
        # Two live rows deliberately exercise global factor replay protection,
        # independently of the additional new-login supersession policy.
        MFAChallenge.objects.create(
            user=user,
            device=DeviceFactory(user=user),
            token_hash=mfa.digest(token, "challenge"),
            credential_state=mfa.state(user),
            expires_at=timezone.now() + mfa.CHALLENGE_TTL,
        )
    if same_challenge:
        tokens[1] = tokens[0]
    proof = codes[0] if factor == "recovery" else mfa.totp(seed, step)
    barrier = Barrier(2)

    def complete(token: str) -> bool:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                mfa.complete(
                    mfa_token=token, code=proof, source_ip=f"2001:db8::{uuid4().int % 65535:x}"
                )
            except (InvalidCredentials, InvalidOTP):
                return False
            return True
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(complete, token) for token in tokens]
        outcomes = [job.result(timeout=20) for job in jobs]
    assert outcomes.count(True) == 1
    assert UserSession.objects.filter(revoked_at__isnull=True, mfa_verified=True).count() == 1
    if factor == "recovery":
        assert MFARecoveryCode.objects.filter(consumed_at__isnull=False).count() == 1
