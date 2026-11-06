"""Legacy sessions are never marked MFA-authenticated by migration."""

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


def test_populated_mfa_migration_preserves_data_without_inventing_factor_proof() -> None:
    previous = [("accounts", "0003_session_families")]
    current = [("accounts", "0004_mfa")]
    executor = MigrationExecutor(connection)
    executor.migrate(previous)
    try:
        old_apps = executor.loader.project_state(previous).apps
        users = old_apps.get_model("accounts", "User")
        sessions = old_apps.get_model("accounts", "UserSession")
        user = users.objects.create(
            phone="+2348031234567",
            full_name="MFA migration fixture",
            password="!",
            two_factor_enabled=True,
            two_factor_method="TOTP",
        )
        session = sessions.objects.create(
            user=user,
            refresh_token_hash="migration-fixture-mfa",
            expires_at=timezone.now() + timedelta(days=30),
        )
        executor = MigrationExecutor(connection)
        executor.migrate(current)
        new_apps = executor.loader.project_state(current).apps
        new_user = new_apps.get_model("accounts", "User").objects.get(pk=user.pk)
        new_session = new_apps.get_model("accounts", "UserSession").objects.get(pk=session.pk)
        assert new_user.two_factor_enabled and new_user.totp_secret == ""
        assert new_user.mfa_version == 0 and new_user.last_totp_step == -1
        assert not new_session.mfa_verified and new_session.mfa_version == 0
        assert (
            new_session.public_id == session.public_id
            and new_session.family_id == session.family_id
        )
        assert new_session.refresh_token_hash == session.refresh_token_hash
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
