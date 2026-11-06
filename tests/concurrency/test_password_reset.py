"""A recovery token can authorize only one concurrent password replacement."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from django.core import mail
from django.db import close_old_connections

from apps.accounts.models import VerificationToken
from apps.accounts.services import recovery
from apps.accounts.services.errors import InvalidOTP
from tests.factories import UserFactory

pytestmark = [
    pytest.mark.concurrency,
    pytest.mark.security,
    pytest.mark.django_db(transaction=True),
]


def test_a_reset_token_has_one_winner_under_concurrent_confirmation() -> None:
    user = UserFactory(
        phone=f"+234803{uuid4().int % 10**7:07d}",
        email=f"concurrent-{uuid4().hex}@example.test",
    )
    assert user.email is not None
    recovery.request_reset(identifier=user.email, source_ip="2001:db8::1")
    token = mail.outbox[-1].body.split(" is ", 1)[1].split(". It expires", 1)[0]
    barrier = Barrier(2)
    candidates = ["rT9!mP4@vL7#xQ2", "bQ4$nW8*eR2!yT6u"]

    def confirm(password: str) -> bool:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                recovery.confirm_reset(
                    reset_token=token, new_password=password, source_ip="2001:db8::2"
                )
            except InvalidOTP:
                return False
            return True
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(confirm, password) for password in candidates]
        outcomes = [job.result(timeout=15) for job in jobs]
    assert outcomes.count(True) == 1
    user.refresh_from_db()
    assert user.check_password(candidates[outcomes.index(True)])
    assert VerificationToken.objects.get().consumed_at is not None
