"""Ownership facts and emailed proof lifecycle serialize under the user lock."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from django.core import mail
from django.db import close_old_connections

from apps.accounts.constants import OTPPurpose, VerificationStatus
from apps.accounts.models import VerificationToken
from apps.accounts.services import email_verification, onboarding, otp
from apps.accounts.services.errors import InvalidOTP
from tests.factories import UserFactory

pytestmark = [
    pytest.mark.concurrency,
    pytest.mark.security,
    pytest.mark.django_db(transaction=True),
]


def _user():
    return UserFactory(
        email=f"concurrent-verification-{uuid4().hex}@example.test",
        phone=f"+234803{uuid4().int % 10**7:07d}",
        is_email_verified=False,
    )


def _request(user):
    email_verification.request_verification(
        user=user, source_ip=f"2001:db8::{uuid4().int % 65535:x}"
    )
    return mail.outbox[-1].body.split(" is ", 1)[1].split(". It expires", 1)[0]


def _run_together(actions):
    barrier = Barrier(len(actions))

    def run(action):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                return action()
            except InvalidOTP:
                return False
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=len(actions)) as executor:
        jobs = [executor.submit(run, action) for action in actions]
        return [job.result(timeout=20) for job in jobs]


def test_email_verification_token_has_one_concurrent_winner() -> None:
    user = _user()
    token = _request(user)

    def confirm():
        email_verification.verify(token=token, source_ip=f"2001:db8::{uuid4().int % 65535:x}")
        return True

    outcomes = _run_together([confirm, confirm])
    assert outcomes.count(True) == 1
    user.refresh_from_db()
    assert user.is_email_verified and VerificationToken.objects.get().consumed_at


def test_concurrent_resends_leave_only_one_usable_email_token() -> None:
    user = _user()

    def resend():
        email_verification.request_verification(
            user=user, source_ip=f"2001:db8::{uuid4().int % 65535:x}"
        )
        return True

    assert _run_together([resend, resend]) == [True, True]
    assert VerificationToken.objects.filter(user=user).count() == 2
    assert VerificationToken.objects.filter(user=user, consumed_at__isnull=True).count() == 1
    secrets = [
        message.body.split(" is ", 1)[1].split(". It expires", 1)[0] for message in mail.outbox
    ]
    outcomes = []
    for token in secrets:
        try:
            email_verification.verify(token=token, source_ip=f"2001:db8::{uuid4().int % 65535:x}")
        except InvalidOTP:
            outcomes.append(False)
        else:
            outcomes.append(True)
    assert outcomes.count(True) == 1


def test_simultaneous_phone_and_email_confirmation_preserve_both_facts() -> None:
    user = _user()
    user.is_phone_verified = False
    user.save(update_fields=["is_phone_verified"])
    token = _request(user)
    code = otp.issue(destination=user.phone, purpose=OTPPurpose.PHONE_VERIFY, user=user).code

    def confirm_email():
        email_verification.verify(token=token, source_ip=f"2001:db8::{uuid4().int % 65535:x}")
        return True

    def confirm_phone():
        onboarding.verify_phone(
            phone=user.phone, code=code, source_ip=f"2001:db8::{uuid4().int % 65535:x}"
        )
        return True

    assert _run_together([confirm_email, confirm_phone]) == [True, True]
    user.refresh_from_db()
    assert user.is_email_verified and user.is_phone_verified
    assert user.verification_status == VerificationStatus.FULLY_VERIFIED
