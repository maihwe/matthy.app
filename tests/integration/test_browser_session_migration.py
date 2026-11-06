"""Populated legacy rows keep their token audience and never gain browser proof."""

from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.django_db(transaction=True),
]


def test_browser_migration_preserves_token_sessions_and_challenges():
    previous = [("accounts", "0004_mfa")]
    current = [("accounts", "0005_browser_mfa")]
    executor = MigrationExecutor(connection)
    executor.migrate(previous)
    try:
        old_apps = executor.loader.project_state(previous).apps
        user = old_apps.get_model("accounts", "User").objects.create(
            phone="+2348031234567", full_name="Browser migration fixture", password="!"
        )
        device = old_apps.get_model("accounts", "Device").objects.create(
            user=user, device_fingerprint="browser-migration", platform="WEB"
        )
        session = old_apps.get_model("accounts", "UserSession").objects.create(
            user=user,
            device=device,
            refresh_token_hash="browser-migration-refresh",
            mfa_verified=True,
            expires_at=timezone.now() + timedelta(days=30),
        )
        challenge = old_apps.get_model("accounts", "MFAChallenge").objects.create(
            user=user,
            device=device,
            token_hash="browser-migration-challenge",
            credential_state="legacy",
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        executor = MigrationExecutor(connection)
        executor.migrate(current)
        new_apps = executor.loader.project_state(current).apps
        rows = new_apps.get_model("accounts", "UserSession")
        proofs = new_apps.get_model("accounts", "MFAChallenge")
        row = rows.objects.get(pk=session.pk)
        proof = proofs.objects.get(pk=challenge.pk)
        assert (
            row.kind == "TOKEN"
            and row.browser_session_key_hash == row.browser_credential_state == ""
        )
        assert row.public_id == session.public_id and row.family_id == session.family_id
        assert proof.audience == "TOKEN" and proof.browser_session_key_hash == ""
        with pytest.raises(IntegrityError), transaction.atomic():
            rows.objects.filter(pk=row.pk).update(kind="BROWSER")
        with pytest.raises(IntegrityError), transaction.atomic():
            proofs.objects.filter(pk=proof.pk).update(audience="BROWSER")
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
