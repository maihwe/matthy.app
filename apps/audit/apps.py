"""The ``audit`` module — the append-only record of who did what (FR-AUD-001…003).

Layer: **L0**, because everything writes to it. It imports nothing from other
project modules.

Two properties make this module different from every other one:

* **It is write-only.** No service exposes an update or delete path, and the
  database enforces that with a trigger rather than trusting the application
  (`apps/audit/migrations/0002_audit_immutability_trigger.py`, 03 §12.1).
* **A failed write is a failed operation.** An audit record is not telemetry
  that may be dropped under load — §44 rule 8 requires every important financial
  action to be recorded, so an audit write happens inside the same transaction
  as the action it describes. If the record cannot be written, the action does
  not happen.
"""

from __future__ import annotations

from django.apps import AppConfig


class AuditConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.audit"
    label = "audit"
    verbose_name = "Audit"
