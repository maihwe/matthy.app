"""Concurrent resends must leave one usable phone code per member."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from django.db import close_old_connections

from apps.accounts.constants import OTPPurpose
from apps.accounts.models import OTPCode, User
from apps.accounts.services import otp
from tests.factories import UnverifiedUserFactory

pytestmark = [
    pytest.mark.concurrency,
    pytest.mark.security,
    pytest.mark.django_db(transaction=True),
]


def test_concurrent_resends_leave_only_the_newest_code_live() -> None:
    # Redis outlives the test database. A factory sequence would reuse this
    # destination on the next run and exhaust its real issuance budget.
    user = UnverifiedUserFactory(phone=f"+234803{uuid4().int % 10**7:07d}")
    barrier = Barrier(2)

    def issue() -> int:
        close_old_connections()
        try:
            current = User.objects.get(pk=user.pk)
            barrier.wait(timeout=10)
            return otp.issue(
                destination=current.phone, purpose=OTPPurpose.PHONE_VERIFY, user=current
            ).record.pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(issue) for _ in range(2)]
        ids = [job.result(timeout=15) for job in jobs]

    rows = OTPCode.objects.filter(pk__in=ids)
    assert rows.count() == 2
    assert rows.filter(consumed_at__isnull=True).count() == 1
