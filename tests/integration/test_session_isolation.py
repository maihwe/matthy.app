"""Refresh revocation must stop the affected login without harming a later one."""

from __future__ import annotations

import pytest

from apps.accounts.constants import AccountStatus
from apps.accounts.models import User
from apps.accounts.services.errors import InvalidRefreshToken, RefreshTokenReuse
from apps.accounts.services.sessions import issue, revoke_session_family, rotate
from tests.factories import DeviceFactory, UserFactory

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.django_db]


def test_a_logged_out_token_does_not_revoke_a_later_login() -> None:
    device = DeviceFactory()
    old = issue(user=device.user, device=device)
    revoke_session_family(old.session)
    current = issue(user=device.user, device=device)

    with pytest.raises(InvalidRefreshToken) as caught:
        rotate(refresh_token=old.refresh_token)

    current.session.refresh_from_db()
    assert current.session.revoked_at is None
    assert not isinstance(caught.value, RefreshTokenReuse)


def test_reuse_revokes_the_rotation_chain_and_preserves_a_separate_login() -> None:
    device = DeviceFactory()
    original = issue(user=device.user, device=device)
    independent = issue(user=device.user, device=device)
    successor = rotate(refresh_token=original.refresh_token)

    with pytest.raises(RefreshTokenReuse):
        rotate(refresh_token=original.refresh_token)

    successor.session.refresh_from_db()
    independent.session.refresh_from_db()
    assert successor.session.revoked_at is not None
    assert independent.session.revoked_at is None


@pytest.mark.parametrize("status", [AccountStatus.SUSPENDED, AccountStatus.CLOSED])
def test_an_inactive_account_cannot_refresh(status: str) -> None:
    user = UserFactory()
    issued = issue(user=user)
    User.objects.filter(pk=user.pk).update(account_status=status)

    with pytest.raises(InvalidRefreshToken):
        rotate(refresh_token=issued.refresh_token)

    issued.session.refresh_from_db()
    assert issued.session.revoked_at is not None


def test_a_session_cannot_be_bound_to_another_members_device() -> None:
    with pytest.raises(InvalidRefreshToken):
        issue(user=UserFactory(), device=DeviceFactory())
