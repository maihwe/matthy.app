"""The ``accounts`` module — identity, credentials, devices and sessions.

This is the **first project module** and must stay so (PHASE_1_PLAN P1-5):
Django cannot swap ``AUTH_USER_MODEL`` once ``auth`` and ``admin`` have been
migrated, because ``admin.LogEntry`` fixes a foreign key to whatever user model
existed at the first ``migrate``. Pointing ``AUTH_USER_MODEL`` at this module's
``User`` after that raises ``InconsistentMigrationHistory``, and the only cure
is dropping the database.

Layer: **L0**. It imports nothing from other project modules (02 §4.1).
"""

from __future__ import annotations

from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    # Django derives the app label from the final component; stated explicitly
    # because `AUTH_USER_MODEL = "accounts.User"` and every `related_name` in
    # the project depend on it, and a silent change to `apps_accounts` would
    # break migrations in a way the error message does not explain.
    label = "accounts"
    verbose_name = "Accounts"
