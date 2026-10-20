"""Celery application.

02 §5.5: Celery + Redis for email, SMS, push, reconciliation, reminders and
report generation. No expensive background work runs inside a request/response
cycle.

Tasks are thin wrappers. A task's body calls a service function; it does not
contain business logic (02 §5.5). That boundary is what keeps every rule
testable without a running worker.
"""

from __future__ import annotations

import os
from typing import Any

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("ihweza")

# Configuration is namespaced under CELERY_ in the Django settings, so broker
# and queue topology stay with the rest of the configuration rather than being
# split across two files.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Discover tasks.py in every installed app.
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)  # type: ignore[untyped-decorator]
def debug_task(self: Any) -> str:  # pragma: no cover - diagnostic only
    """Round-trip check that the broker is reachable."""
    return f"request: {self.request!r}"
