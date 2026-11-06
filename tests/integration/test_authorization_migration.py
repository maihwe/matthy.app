"""Authorization metadata preserves populated zero-version legacy sessions."""

from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.django_db(transaction=True),
]


def test_authorization_migration_preserves_legacy_bearer_and_browser_rows():
    previous = [("accounts", "0005_browser_mfa")]
    current = [("accounts", "0006_user_authorization_version_user_role_mfa_required_and_more")]
    executor = MigrationExecutor(connection)
    executor.migrate(previous)
    try:
        old = executor.loader.project_state(previous).apps
        user = old.get_model("accounts", "User").objects.create(
            phone="+2348031234567", full_name="Authorization migration", password="!"
        )
        device = old.get_model("accounts", "Device").objects.create(
            user=user, device_fingerprint="authorization-migration", platform="WEB"
        )
        model = old.get_model("accounts", "UserSession")
        bearer = model.objects.create(
            user=user,
            device=device,
            refresh_token_hash="authorization-token-fixture",
            expires_at=timezone.now() + timedelta(days=30),
        )
        browser = model.objects.create(
            user=user,
            device=device,
            refresh_token_hash="authorization-browser-fixture",
            kind="BROWSER",
            browser_session_key_hash="legacy-cookie",
            browser_credential_state="legacy-digest",
            mfa_verified=True,
            expires_at=timezone.now() + timedelta(hours=12),
        )
        executor = MigrationExecutor(connection)
        executor.migrate(current)
        new = executor.loader.project_state(current).apps
        subject = new.get_model("accounts", "User").objects.get(pk=user.pk)
        assert subject.authorization_version == 0 and not subject.role_mfa_required
        rows = new.get_model("accounts", "UserSession")
        for prior in (bearer, browser):
            row = rows.objects.get(pk=prior.pk)
            assert row.authorization_version == 0 and row.revoked_at is None
            assert row.kind == prior.kind and row.family_id == prior.family_id
            assert row.browser_credential_state == prior.browser_credential_state
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
