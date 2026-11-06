"""The session migration must work with populated tables, not just a fresh database."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def test_public_device_ids_and_legacy_token_families_are_backfilled() -> None:
    previous = [("accounts", "0002_userprofile_location")]
    current = [("accounts", "0003_session_families")]
    executor = MigrationExecutor(connection)
    executor.migrate(previous)
    try:
        old_apps = executor.loader.project_state(previous).apps
        user_model = old_apps.get_model("accounts", "User")
        device_model = old_apps.get_model("accounts", "Device")
        session_model = old_apps.get_model("accounts", "UserSession")
        user = user_model.objects.create(
            phone="+2348031234567", full_name="Migration fixture", password="!"
        )
        first = device_model.objects.create(
            user=user, device_fingerprint="first-fixture", platform="ANDROID"
        )
        second = device_model.objects.create(
            user=user, device_fingerprint="second-fixture", platform="IOS"
        )
        stamp = timezone.now()
        old = session_model.objects.create(
            user=user,
            device=first,
            refresh_token_hash="fixture-predecessor",
            issued_at=stamp,
            expires_at=stamp + timedelta(days=30),
            revoked_at=stamp,
        )
        successor = session_model.objects.create(
            user=user,
            device=first,
            refresh_token_hash="fixture-successor",
            issued_at=stamp,
            expires_at=stamp + timedelta(days=30),
        )
        other = session_model.objects.create(
            user=user,
            device=second,
            refresh_token_hash="fixture-independent",
            issued_at=stamp,
            expires_at=stamp + timedelta(days=30),
        )

        executor = MigrationExecutor(connection)
        executor.migrate(current)
        new_apps = executor.loader.project_state(current).apps
        devices = new_apps.get_model("accounts", "Device")
        sessions = new_apps.get_model("accounts", "UserSession")
        assert len(set(devices.objects.values_list("public_id", flat=True))) == 2
        old_row, successor_row, other_row = (
            sessions.objects.get(pk=row.pk) for row in (old, successor, other)
        )
        assert old_row.family_id == successor_row.family_id
        assert old_row.family_id != other_row.family_id
        assert old_row.revoked_at == stamp
        assert successor_row.revoked_at is None
        assert old_row.refresh_token_hash == "fixture-predecessor"
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
