"""Zero-version digests survive S5; any role-version change invalidates them."""

import hashlib
import json

import pytest
from django.utils.crypto import salted_hmac

from apps.accounts.services import browser, mfa
from tests.factories import UserFactory

pytestmark = [pytest.mark.unit, pytest.mark.security]


def test_zero_version_browser_digest_preserves_the_pre_group_cookie_binding():
    user = UserFactory.build()
    legacy_fields = [
        str(user.public_id),
        user.password,
        user.pin_hash,
        user.mfa_version,
        user.two_factor_enabled,
        user.two_factor_method,
        user.is_staff,
        user.is_superuser,
        user.phone,
        user.is_phone_verified,
    ]
    legacy = hashlib.sha256(
        b"ihweza.browser-credentials.v1:" + json.dumps(legacy_fields).encode()
    ).hexdigest()
    assert browser.credential_state(user) == legacy
    user.authorization_version = 1
    assert browser.credential_state(user) != legacy
    version_digest = browser.credential_state(user)
    user.role_mfa_required = True
    assert browser.credential_state(user) != version_digest


def test_zero_version_mfa_proof_preserves_legacy_state_until_roles_change():
    user = UserFactory.build()
    legacy_value = (
        f"{user.get_session_auth_hash()}:{user.two_factor_enabled}:"
        f"{user.two_factor_method}:{user.is_staff}:{user.is_superuser}:"
        f"{user.phone}:{user.is_phone_verified}"
    )
    legacy = salted_hmac("ihweza.mfa.state.v1", legacy_value, algorithm="sha256").hexdigest()
    assert mfa.state(user) == legacy
    user.authorization_version = 1
    assert mfa.state(user) != legacy
    hash_after_role_change = user.get_session_auth_hash()
    user.authorization_version = 2
    assert user.get_session_auth_hash() != hash_after_role_change
